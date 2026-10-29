"""Consumer-visible WebSocket tests against real Uvicorn and stateful children."""

import asyncio
import json
import logging
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from acp_bridge.app import create_app
from acp_bridge.config import Settings

TOKEN = "test-device-token-with-more-than-forty-three-ascii-characters"
AUTH = [("Authorization", f"Bearer {TOKEN}")]
FIXTURE = Path(__file__).with_name("agent_fixture.py")


async def until(predicate, seconds=4):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


def launches(root):
    path = root / "launches"
    return [int(value) for value in path.read_text().splitlines()] if path.exists() else []


def received(root):
    path = root / "received.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def running_pid(pid):
    try:
        # A killed descendant can briefly be a zombie under the system reaper.
        status = Path(f"/proc/{pid}/stat").read_text()
        return status[status.rfind(")") + 2:].split()[0] != "Z"
    except FileNotFoundError:
        return False


@asynccontextmanager
async def live(root, mode="normal", **overrides):
    values = {
        "device_token": TOKEN,
        "agent_command": (sys.executable, str(FIXTURE), str(root), mode),
        "working_directory": "/admin-owned/workspace",
        "max_frame_bytes": 4096,
        "startup_seconds": 2,
        "idle_seconds": 3,
        "session_seconds": 10,
        "io_seconds": 0.5,
        "terminate_seconds": 0.05,
    }
    values.update(overrides)
    app = create_app(Settings(**values))
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="on", log_config=None,
        log_level="critical", access_log=False, ws="websockets-sansio",
        ws_max_size=values["max_frame_bytes"] * 2, ws_max_queue=4,
        ws_per_message_deflate=False,
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await until(lambda: server.started or serving.done())
        if serving.done():
            await serving
            pytest.fail("ASGI server exited during startup")
        yield app, f"ws://127.0.0.1:{port}/acp", port
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


async def send(ws, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not ...:
        message["id"] = request_id
    await ws.send(json.dumps(message))


async def receive(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 3))


async def start_session(ws):
    await send(ws, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}, "init")
    initialized = await receive(ws)
    assert initialized["id"] == "init"
    assert initialized["result"]["protocolVersion"] == 1
    assert initialized["result"]["agentCapabilities"]["mcpCapabilities"]["http"] is True
    await send(ws, "authenticate", {"methodId": "agent"}, "auth")
    assert await receive(ws) == {"jsonrpc": "2.0", "id": "auth", "result": {}}
    await send(ws, "session/new", {"cwd": "/", "mcpServers": []}, "new")
    created = await receive(ws)
    return created["result"]["sessionId"]


async def assert_reaped(root, expected=1):
    await until(lambda: len(launches(root)) == expected)
    await until(lambda: all(not Path(f"/proc/{pid}").exists() for pid in launches(root)))
    # The relay is the direct parent: these should be fully reaped, not zombies.
    for pid in launches(root):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


async def test_health_is_public_and_old_routes_are_removed(tmp_path):
    async with live(tmp_path) as (_, _, port):
        for method, path, expected in [("GET", "/healthz", 200), ("POST", "/v1/text", 404), ("POST", "/v1/turn", 404)]:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            head, body = response.split(b"\r\n\r\n", 1)
            assert f" {expected} ".encode() in head
            if expected == 200:
                assert json.loads(body) == {"status": "ok"}
        assert launches(tmp_path) == []


@pytest.mark.parametrize("headers", [
    [], [("Authorization", "Bearer wrong")], [("Authorization", "Basic " + TOKEN)],
    [("Authorization", "Bearer " + TOKEN + " extra")], AUTH * 2,
    [("Authorization", "Bearer ")],
])
async def test_auth_denied_before_accept_and_process(tmp_path, headers):
    async with live(tmp_path) as (_, url, _):
        with pytest.raises(InvalidStatus) as denied:
            async with connect(url, additional_headers=headers):
                pytest.fail("Unauthorized connection accepted")
        assert denied.value.response.status_code == 401
        assert launches(tmp_path) == []


async def test_complete_bidirectional_acp_flow_preserves_ids_and_payloads(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("BRIDGE_DEVICE_TOKEN", "must-not-inherit")
    async with live(tmp_path) as (_, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            session_id = await start_session(ws)
            await send(ws, "session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Opaque user content; do not interpret."}],
            }, "turn-opaque-42")
            update = await receive(ws)
            assert update == {"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk", "content": {
                    "type": "text", "text": "A fixture response. ",
                }},
            }}
            permission = await receive(ws)
            assert permission["method"] == "session/request_permission"
            reply = {"jsonrpc": "2.0", "id": permission["id"], "result": {"outcome": {"outcome": "cancelled"}}}
            await ws.send(json.dumps(reply))
            assert (await receive(ws))["params"]["update"]["content"]["text"] == "Permission denied."
            assert await receive(ws) == {"jsonrpc": "2.0", "id": "turn-opaque-42", "result": {"stopReason": "end_turn"}}
            # Valid responses, including unrecognized and null IDs, belong to the
            # agent's RPC dispatcher. The transport must not invent a response.
            unknown = {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "Unavailable", "data": {"detail": 4}}}
            await ws.send(json.dumps(unknown))
            await send(ws, "session/close", {"sessionId": session_id}, 0)
            assert await receive(ws) == {"jsonrpc": "2.0", "id": 0, "result": {}}
            assert reply in received(tmp_path)
            assert unknown in received(tmp_path)
            created = next(item for item in received(tmp_path) if item.get("method") == "session/new")
            assert created["params"] == {"cwd": "/admin-owned/workspace", "mcpServers": []}
        await assert_reaped(tmp_path)
        environment = json.loads(next(tmp_path.glob("environment-*")).read_text())
        assert environment == {"cwd": "/", "device_token_inherited": False}
    assert "fixture-private-stderr-secret" not in caplog.text
    assert "Opaque user content" not in caplog.text


async def test_cancel_notification_reaches_agent_and_disconnect_releases_slot(tmp_path):
    async with live(tmp_path) as (app, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            session_id = await start_session(ws)
            await send(ws, "session/prompt", {"sessionId": session_id, "prompt": []}, 17)
            await receive(ws)
            await receive(ws)
            await send(ws, "session/cancel", {"sessionId": session_id}, ...)
            assert await receive(ws) == {"jsonrpc": "2.0", "id": 17, "result": {"stopReason": "cancelled"}}
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)
        async with connect(url, additional_headers=AUTH) as ws:
            assert await start_session(ws) == "fixture-1"
        await assert_reaped(tmp_path, expected=2)


async def test_only_one_concurrent_connection_and_slot_held_during_teardown(tmp_path):
    async with live(tmp_path, mode="slow-stop", terminate_seconds=0.4) as (app, url, _):
        ws = await connect(url, additional_headers=AUTH)
        await start_session(ws)
        first_pid = launches(tmp_path)[0]
        with pytest.raises(InvalidStatus) as busy:
            async with connect(url, additional_headers=AUTH):
                pytest.fail("Second client accepted")
        assert busy.value.response.status_code == 429
        assert launches(tmp_path) == [first_pid]
        await ws.close()
        await until(lambda: (tmp_path / f"stopping-{first_pid}").exists())
        with pytest.raises(InvalidStatus) as stopping:
            async with connect(url, additional_headers=AUTH):
                pytest.fail("Admission reopened before child teardown")
        assert stopping.value.response.status_code == 429
        assert app.state.active.locked()
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)
        async with connect(url, additional_headers=AUTH) as next_ws:
            await start_session(next_ws)
        await assert_reaped(tmp_path, expected=2)


async def test_simultaneous_admission_has_exactly_one_winner(tmp_path):
    async with live(tmp_path) as (app, url, _):
        async def enter():
            try:
                return await connect(url, additional_headers=AUTH)
            except InvalidStatus as exc:
                return exc.response.status_code
        outcomes = await asyncio.gather(*(enter() for _ in range(8)))
        winners = [item for item in outcomes if item != 429]
        assert len(winners) == 1
        await start_session(winners[0])
        assert len(launches(tmp_path)) == 1
        await winners[0].close()
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)


@pytest.mark.parametrize("mcp_servers", [
    [{"name": "arbitrary", "command": "not-allowed", "args": [], "env": []}],
    [{"type": "http", "name": "arbitrary", "url": "https://untrusted.invalid/mcp", "headers": []}],
    {}, "", None, False,
])
async def test_client_cannot_supply_mcp_servers(tmp_path, mcp_servers):
    async with live(tmp_path) as (_, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            await start_session(ws)
            count = len(received(tmp_path))
            await send(ws, "session/new", {"cwd": "/injected", "mcpServers": mcp_servers}, "denied")
            assert await receive(ws) == {"jsonrpc": "2.0", "id": "denied", "error": {"code": -32602, "message": "Invalid params"}}
            await send(ws, "session/new", {"cwd": "/injected"}, "safe")
            assert (await receive(ws))["result"]["sessionId"] == "fixture-2"
            forwarded = received(tmp_path)[count:]
            assert len(forwarded) == 1
            assert forwarded[0]["params"] == {"cwd": "/admin-owned/workspace", "mcpServers": []}
        await assert_reaped(tmp_path)


@pytest.mark.parametrize("request_id", ["unusual-id", 0, 12.5, None])
async def test_disallowed_methods_use_standard_errors_and_preserve_ids(tmp_path, request_id):
    async with live(tmp_path) as (_, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            await send(ws, "session/load", {"cwd": "/injected"}, request_id)
            assert await receive(ws) == {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            await send(ws, "fs/read_text_file", {}, ...)
            await send(ws, "initialize", {"protocolVersion": 1}, "after-notification")
            assert (await receive(ws))["id"] == "after-notification"
            assert [message["method"] for message in received(tmp_path)] == ["initialize"]
        await assert_reaped(tmp_path)


@pytest.mark.parametrize("frame, code, close_code", [
    (b"binary", -32600, 1003),
    ("{", -32700, 1007),
    ('{"jsonrpc":"2.0","method":"initialize","id":1,"id":2}', -32700, 1007),
    ('{"jsonrpc":"2.0","method":"initialize","params":{"x":NaN}}', -32700, 1007),
    ('{"jsonrpc":"2.0","method":"initialize","params":{"x":1e999}}', -32700, 1007),
    ('{"jsonrpc":"2.0","method":"initialize","params":{"x":"\\ud800"}}', -32700, 1007),
    ("[]", -32600, 1008),
    ('[{"jsonrpc":"2.0","method":"initialize","id":1}]', -32600, 1008),
    ("null", -32600, 1008),
    ('{"jsonrpc":"1.0","method":"initialize","id":1}', -32600, 1008),
    ('{"jsonrpc":"2.0","method":"initialize","id":true}', -32600, 1008),
    ('{"jsonrpc":"2.0","method":"initialize","id":{}}', -32600, 1008),
    ('{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":1,"message":"bad"}}', -32600, 1008),
    ('{"jsonrpc":"2.0","id":1,"error":{"code":true,"message":"bad"}}', -32600, 1008),
    ('{"jsonrpc":"2.0","result":{}}', -32600, 1008),
    ('{"jsonrpc":"2.0","method":"initialize","params":"not-structured"}', -32600, 1008),
    ("x" * 4097, -32600, 1009),
    ("é" * 2049, -32600, 1009),
])
async def test_invalid_client_frames_fail_cleanly_and_reap_child(tmp_path, frame, code, close_code):
    async with live(tmp_path) as (app, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            await until(lambda: bool(launches(tmp_path)))
            await ws.send(frame)
            error = await receive(ws)
            assert error["jsonrpc"] == "2.0"
            assert error["error"]["code"] == code
            with pytest.raises(ConnectionClosed) as closed:
                await ws.recv()
            assert closed.value.rcvd.code == close_code
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)
        assert received(tmp_path) == []


@pytest.mark.parametrize("mode", ["bad-json", "bad-envelope", "long-line", "unterminated", "invalid-utf8", "exit"])
async def test_invalid_agent_output_or_exit_is_private_and_reaped(tmp_path, mode, caplog):
    async with live(tmp_path, mode=mode) as (app, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            await send(ws, "initialize", {"protocolVersion": 1})
            if mode in {"bad-json", "bad-envelope", "invalid-utf8"}:
                assert await receive(ws) == {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal error"}}
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 3)
            assert closed.value.rcvd.code == 1011
            assert "private" not in closed.value.rcvd.reason
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)
    assert "fixture-private-stderr-secret" not in caplog.text


@pytest.mark.parametrize("kind", ["startup", "idle", "session"])
async def test_finite_deadlines_release_admission_and_reap(tmp_path, kind):
    limits = {"startup_seconds": 0.3} if kind == "startup" else {
        "startup_seconds": 2, "idle_seconds": 0.35 if kind == "idle" else 3,
        "session_seconds": 0.5 if kind == "session" else 10,
    }
    async with live(tmp_path, **limits) as (app, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            if kind != "startup":
                await start_session(ws)
            if kind == "session":
                # Activity must not extend the absolute subprocess lifetime.
                async def activity():
                    while True:
                        await send(ws, "session/close", {"sessionId": "missing"})
                        await receive(ws)
                        await asyncio.sleep(0.04)
                with pytest.raises(ConnectionClosed) as closed:
                    await activity()
            else:
                with pytest.raises(ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 3)
            assert closed.value.rcvd.code == 1008
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)


async def test_blocked_agent_stdin_hits_io_deadline_and_reaps(tmp_path):
    async with live(tmp_path, mode="no-read", max_frame_bytes=131_072, startup_seconds=3, io_seconds=0.15) as (app, url, _):
        async with connect(url, additional_headers=AUTH, max_size=None) as ws:
            message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {
                "sessionId": "opaque", "prompt": [{"type": "text", "text": "x" * 120_000}],
            }})
            for _ in range(4):
                try:
                    await ws.send(message)
                except ConnectionClosed:
                    break
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 2)
            assert closed.value.rcvd.code == 1008
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)


async def test_disconnect_kills_stubborn_process_group_and_reaps_leader(tmp_path):
    async with live(tmp_path, mode="ignore-term") as (app, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            await start_session(ws)
            pid = launches(tmp_path)[0]
            descendant_file = tmp_path / f"descendant-{pid}"
            await until(descendant_file.exists)
            descendant = int(descendant_file.read_text())
            assert running_pid(descendant)
        await until(lambda: not app.state.active.locked())
        await assert_reaped(tmp_path)
        await until(lambda: not running_pid(descendant))


async def test_spawn_failure_does_not_leak_argv_or_hold_slot(tmp_path):
    async with live(tmp_path, agent_command=("/nonexistent/private-command-secret",)) as (app, url, _):
        for _ in range(2):
            async with connect(url, additional_headers=AUTH) as ws:
                with pytest.raises(ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 3)
                assert closed.value.rcvd.code == 1011
                assert closed.value.rcvd.reason == "Agent unavailable"
            await until(lambda: not app.state.active.locked())
        assert launches(tmp_path) == []


async def test_admin_argv_is_execed_without_shell_interpretation(tmp_path):
    injected_file = tmp_path / "must-not-exist"
    async with live(tmp_path, mode=f"normal; touch {injected_file}") as (_, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            await start_session(ws)
        await assert_reaped(tmp_path)
        assert not injected_file.exists()


async def test_server_owned_mcp_bootstrap_is_injected_without_exposure(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    servers = ({
        "name": "generic-agent-tool", "command": "/remote/admin-owned/mcp",
        "args": ["literal argument"], "env": [{"name": "GENERIC_CREDENTIAL", "value": "private-bootstrap-sentinel"}],
    }, {
        "type": "http", "name": "generic-http-tool", "url": "https://tools.invalid/mcp",
        "headers": [{"name": "Authorization", "value": "private-header-sentinel"}],
    })
    async with live(tmp_path, mcp_servers=servers) as (_, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            assert await start_session(ws) == "fixture-1"
            # A second session with absent mcpServers must get the same owned
            # bootstrap; neither clients nor agent responses learn private config.
            await send(ws, "session/new", {"cwd": "/client-controlled"}, "second")
            response = await receive(ws)
            assert response == {"jsonrpc": "2.0", "id": "second", "result": {"sessionId": "fixture-2"}}
            sessions = [item for item in received(tmp_path) if item.get("method") == "session/new"]
            assert len(sessions) == 2
            for item in sessions:
                assert item["params"] == {"cwd": "/admin-owned/workspace", "mcpServers": list(servers)}
            await send(ws, "session/new", {"mcpServers": [servers[0]]}, "client-copy")
            assert (await receive(ws))["error"]["code"] == -32602
        await assert_reaped(tmp_path)
    assert "private-bootstrap-sentinel" not in caplog.text
    assert "private-header-sentinel" not in caplog.text
    assert TOKEN not in caplog.text


async def test_client_error_response_to_server_request_is_forwarded(tmp_path):
    async with live(tmp_path) as (_, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            session_id = await start_session(ws)
            await send(ws, "session/prompt", {"sessionId": session_id, "prompt": []}, "prompt")
            await receive(ws)
            permission = await receive(ws)
            reply = {"jsonrpc": "2.0", "id": permission["id"], "error": {"code": -32601, "message": "Method not found"}}
            await ws.send(json.dumps(reply))
            assert (await receive(ws))["params"]["update"]["content"]["text"] == "Client refused the request."
            assert (await receive(ws))["result"]["stopReason"] == "end_turn"
            assert reply in received(tmp_path)
        await assert_reaped(tmp_path)


async def test_frame_at_exact_byte_limit_is_accepted(tmp_path):
    async with live(tmp_path) as (_, url, _):
        async with connect(url, additional_headers=AUTH) as ws:
            message = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {"protocolVersion": 1, "padding": ""}}
            message["params"]["padding"] = "x" * (4096 - len(json.dumps(message).encode()))
            frame = json.dumps(message)
            assert len(frame.encode()) == 4096
            await ws.send(frame)
            assert (await receive(ws))["result"]["protocolVersion"] == 1
        await assert_reaped(tmp_path)


async def test_server_shutdown_reaps_active_process(tmp_path):
    async with live(tmp_path) as (_, url, _):
        ws = await connect(url, additional_headers=AUTH)
        await start_session(ws)
        assert len(launches(tmp_path)) == 1
    await assert_reaped(tmp_path)
    assert ws.close_code in {1001, 1012}
    await ws.close()
