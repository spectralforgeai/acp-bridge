"""A stateful stdio ACP peer used only by real subprocess regression tests."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
mode = sys.argv[2]
pid = os.getpid()
with (root / "launches").open("a") as file:
    file.write(f"{pid}\n")
(root / f"environment-{pid}").write_text(json.dumps({
    "cwd": os.getcwd(), "device_token_inherited": "BRIDGE_DEVICE_TOKEN" in os.environ,
}))
# Private stderr must never reach the client or server logs, even at failure.
print("fixture-private-stderr-secret", file=sys.stderr, flush=True)

if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    descendant = subprocess.Popen([
        sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(600)",
    ])
    (root / f"descendant-{pid}").write_text(str(descendant.pid))
elif mode == "slow-stop":
    def stop(number, frame):
        (root / f"stopping-{pid}").touch()
        time.sleep(0.2)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)

if mode in {"silent", "no-read"}:
    time.sleep(600)
    sys.exit(0)

initialized = authenticated = False
sessions = set()
pending = None
sequence = 0


def send(message):
    print(json.dumps(message, separators=(",", ":")), flush=True)


def response(request, result):
    if "id" in request:
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})


def error(request, code, message):
    if "id" in request:
        send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": code, "message": message}})


def chunk(text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": pending["params"]["sessionId"],
        "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
    }})


for line in sys.stdin:
    request = json.loads(line)
    with (root / "received.jsonl").open("a") as file:
        file.write(json.dumps(request) + "\n")
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialize":
        if mode == "bad-json":
            print("not-json", flush=True)
            continue
        if mode == "bad-envelope":
            print('{"jsonrpc":"2.0","id":1,"error":{"code":true,"message":"private"}}', flush=True)
            continue
        if mode == "long-line":
            sys.stdout.write("x" * 131_072 + "\n")
            sys.stdout.flush()
            continue
        if mode == "unterminated":
            sys.stdout.write('{"jsonrpc":"2.0","id":1,"result":{}}')
            sys.stdout.flush()
            sys.exit(0)
        if mode == "invalid-utf8":
            sys.stdout.buffer.write(b"\xff\n")
            sys.stdout.buffer.flush()
            continue
        if mode == "exit":
            sys.exit(7)
        initialized = True
        response(request, {
            "protocolVersion": 1,
            "agentCapabilities": {"mcpCapabilities": {"http": True, "sse": True}},
            "authMethods": [{"id": "agent", "name": "Agent"}],
        })
    elif method == "authenticate":
        if not initialized or params.get("methodId") != "agent":
            error(request, -32602, "Invalid authentication state")
            continue
        authenticated = True
        response(request, {})
    elif method == "session/new":
        if not authenticated:
            error(request, -32000, "Authentication required")
            continue
        sequence += 1
        session_id = f"fixture-{sequence}"
        sessions.add(session_id)
        response(request, {"sessionId": session_id})
    elif method == "session/prompt":
        if params.get("sessionId") not in sessions or pending is not None:
            error(request, -32602, "Invalid session state")
            continue
        pending = request
        chunk("A fixture response. ")
        send({"jsonrpc": "2.0", "id": "permission-1", "method": "session/request_permission", "params": {
            "sessionId": params["sessionId"],
            "toolCall": {"toolCallId": "fixture-tool", "title": "Fixture permission"},
            "options": [{"optionId": "once", "name": "Allow once", "kind": "allow_once"}],
        }})
    elif method is None:
        if request.get("id") == "permission-1" and pending is not None:
            if "error" in request:
                chunk("Client refused the request.")
            else:
                chunk("Permission denied.")
            response(pending, {"stopReason": "end_turn"})
            pending = None
    elif method == "session/cancel":
        if pending is not None and params.get("sessionId") == pending["params"]["sessionId"]:
            response(pending, {"stopReason": "cancelled"})
            pending = None
        response(request, {})
    elif method == "session/close":
        sessions.discard(params.get("sessionId"))
        response(request, {})
    else:
        error(request, -32601, "Method not found")
