"""Single-client ACP WebSocket to stdio relay. Run exactly one ASGI worker."""

import asyncio
import hmac
import logging
import os
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from .config import Settings
from .protocol import RPCError, client_policy, decode_message, encode_message


class RelayClosed(Exception):
    def __init__(self, code: int, reason: str):
        self.code = code
        self.reason = reason

class PrivateTransportLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # WebSocket debug/trace logs include frames and handshake credentials.
        return "/websockets/" not in record.pathname.replace("\\", "/")


def silence_transport_logs() -> None:
    for name in ("websockets", "websockets.client", "websockets.server", "websockets.protocol", "uvicorn.asgi"):
        logging.getLogger(name).disabled = True
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, PrivateTransportLogs) for item in logger.filters):
        logger.addFilter(PrivateTransportLogs())



def authorized(websocket: WebSocket, settings: Settings) -> bool:
    values = websocket.headers.getlist("authorization")
    parts = values[0].split(" ") if len(values) == 1 else []
    return (
        len(parts) == 2
        and parts[0].lower() == "bearer"
        and hmac.compare_digest(parts[1].encode("utf-8"), settings.device_token.encode("ascii"))
    )


async def deny(websocket: WebSocket, status: int) -> None:
    if "websocket.http.response" in websocket.scope.get("extensions", {}):
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        await websocket.send_denial_response(JSONResponse(
            {"detail": "Unauthorized" if status == 401 else "Relay is busy"},
            status_code=status, headers=headers,
        ))
    else:
        # ASGI servers without the denial extension turn this into HTTP 403.
        await websocket.close(code=1008)


async def terminate(process: asyncio.subprocess.Process, grace: float) -> None:
    """Kill the whole dedicated process group, even if its leader already exited."""
    def group_signal(number: int) -> None:
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            pass

    # Resume a possibly paused stdout transport so Process.wait cannot deadlock
    # on unread buffered output after its reader task has been cancelled.
    async def discard_stdout():
        if process.stdout is not None:
            while await process.stdout.read(65_536):
                pass

    group_signal(signal.SIGTERM)
    # Descendants may ignore TERM or retain the pipes after the leader exits.
    await asyncio.sleep(grace)
    group_signal(signal.SIGKILL)
    await asyncio.gather(discard_stdout(), process.wait())
    if process.stdin is not None:
        process.stdin.close()


async def relay(websocket: WebSocket, process: asyncio.subprocess.Process, settings: Settings) -> None:
    assert process.stdin is not None and process.stdout is not None
    loop = asyncio.get_running_loop()
    started = last_activity = loop.time()
    agent_ready = False
    send_lock = asyncio.Lock()

    async def send(message: dict) -> None:
        async with asyncio.timeout(settings.io_seconds):
            async with send_lock:
                await websocket.send_text(encode_message(message))

    async def from_client() -> None:
        nonlocal last_activity
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                return
            if frame.get("text") is None:
                await send(RPCError(-32600, "Invalid Request").envelope())
                raise RelayClosed(1003, "Text frames required")
            text = frame["text"]
            try:
                size = len(text.encode("utf-8"))
            except UnicodeError:
                await send(RPCError(-32700, "Parse error").envelope())
                raise RelayClosed(1007, "Invalid UTF-8") from None
            if size > settings.max_frame_bytes:
                await send(RPCError(-32600, "Invalid Request").envelope())
                raise RelayClosed(1009, "Frame too large")
            try:
                message = decode_message(text)
            except RPCError as exc:
                await send(exc.envelope())
                raise RelayClosed(1007 if exc.code == -32700 else 1008, "Invalid JSON-RPC") from None
            last_activity = loop.time()
            try:
                outgoing = client_policy(message, settings.working_directory, settings.mcp_servers)
            except RPCError as exc:
                if "id" in message:
                    await send(exc.envelope())
                continue
            payload = encode_message(outgoing).encode("utf-8")
            if len(payload) > settings.max_frame_bytes:
                if "id" in message:
                    await send(RPCError(-32602, "Invalid params", message["id"]).envelope())
                continue
            async with asyncio.timeout(settings.io_seconds):
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()

    async def from_agent() -> None:
        nonlocal last_activity, agent_ready
        while True:
            try:
                line = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                raise RelayClosed(1011, "Agent transport failure") from None
            if not line:
                raise RelayClosed(1011, "Agent disconnected")
            if not line.endswith(b"\n") or len(line) - 1 > settings.max_frame_bytes:
                raise RelayClosed(1011, "Agent transport failure")
            try:
                message = decode_message(line[:-1].decode("utf-8", errors="strict"))
            except (RPCError, UnicodeError):
                await send(RPCError(-32603, "Internal error").envelope())
                raise RelayClosed(1011, "Agent transport failure") from None
            agent_ready = True
            last_activity = loop.time()
            await send(message)

    async def deadlines() -> None:
        while True:
            now = loop.time()
            remaining = min(
                started + settings.session_seconds - now,
                last_activity + settings.idle_seconds - now,
                settings.startup_seconds if agent_ready else started + settings.startup_seconds - now,
            )
            if remaining <= 0:
                raise RelayClosed(1008, "Relay deadline exceeded")
            await asyncio.sleep(remaining)

    tasks = [asyncio.create_task(fn()) for fn in (from_client, from_agent, deadlines)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Consume all completed exceptions; a simultaneous disconnect is harmless.
        results = await asyncio.gather(*done, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    connections: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        silence_transport_logs()
        application.state.settings = settings if settings is not None else Settings.from_env()
        application.state.active = asyncio.Lock()
        try:
            yield
        finally:
            for task in tuple(connections):
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)

    application = FastAPI(
        title="ACP relay", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
    )

    @application.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @application.websocket("/acp")
    async def acp(websocket: WebSocket):
        configured = application.state.settings
        if not authorized(websocket, configured):
            await deny(websocket, 401)
            return
        slot = application.state.active
        if slot.locked():
            await deny(websocket, 429)
            return
        # An unlocked asyncio.Lock acquires without yielding; admission is atomic.
        await slot.acquire()
        task = asyncio.current_task()
        connections.add(task)
        spawn = None
        process = None
        accepted = False
        close_code, close_reason = 1000, ""
        try:
            async with asyncio.timeout(configured.startup_seconds):
                await websocket.accept()
                accepted = True
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    *configured.agent_command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                    cwd="/",
                    limit=configured.max_frame_bytes + 1,
                    env={key: value for key, value in os.environ.items() if key != "BRIDGE_DEVICE_TOKEN"},
                ))
                # Shield allocation so cancellation cannot orphan an allocated child.
                process = await asyncio.shield(spawn)
            await relay(websocket, process, configured)
        except RelayClosed as exc:
            close_code, close_reason = exc.code, exc.reason
        except WebSocketDisconnect:
            pass
        except TimeoutError:
            close_code, close_reason = 1008, "Relay deadline exceeded"
        except asyncio.CancelledError:
            close_code, close_reason = 1001, "Relay stopping"
            raise
        except Exception:
            # Never expose exception strings, command argv, stderr, or user data.
            close_code, close_reason = 1011, "Agent unavailable"
        finally:
            async def cleanup():
                child = process
                if child is None and spawn is not None:
                    try:
                        child = await spawn
                    except Exception:
                        pass
                if child is not None:
                    await terminate(child, configured.terminate_seconds)
                if accepted:
                    try:
                        async with asyncio.timeout(configured.io_seconds):
                            await websocket.close(code=close_code, reason=close_reason)
                    except (RuntimeError, WebSocketDisconnect, OSError, TimeoutError):
                        pass

            cleanup_task = asyncio.create_task(cleanup())
            try:
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        # The admission slot stays held until teardown actually finishes.
                        continue
                cleanup_task.result()
            finally:
                connections.discard(task)
                slot.release()

    return application


app = create_app()
