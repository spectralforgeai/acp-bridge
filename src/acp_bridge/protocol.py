"""JSON-RPC envelope validation and the narrow ACP client transport policy."""

import json
import math
from typing import Any

ALLOWED_METHODS = frozenset({
    "initialize", "authenticate", "session/new", "session/prompt",
    "session/cancel", "session/close",
})


class RPCError(ValueError):
    def __init__(self, code: int, message: str, request_id: Any = None):
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(message)

    def envelope(self) -> dict:
        return {
            "jsonrpc": "2.0", "id": self.request_id,
            "error": {"code": self.code, "message": self.message},
        }


def valid_id(value: Any) -> bool:
    return (
        value is None or isinstance(value, str) or type(value) is int
        or (type(value) is float and math.isfinite(value))
    )


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


def decode_message(text: str) -> dict:
    try:
        text.encode("utf-8", errors="strict")
        value = json.loads(
            text, object_pairs_hook=unique_object, parse_constant=invalid_constant,
        )
        # Escaped lone surrogates are legal JSON syntax but not valid UTF-8 RPC text.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise RPCError(-32700, "Parse error") from None
    request_id = value.get("id") if isinstance(value, dict) else None
    if not valid_id(request_id):
        request_id = None
    error = RPCError(-32600, "Invalid Request", request_id)
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise error
    if "id" in value and not valid_id(value["id"]):
        raise error
    if "method" in value:
        if (
            not isinstance(value["method"], str)
            or not set(value) <= {"jsonrpc", "id", "method", "params"}
            or ("params" in value and not isinstance(value["params"], (dict, list)))
        ):
            raise error
    else:
        if (
            "id" not in value
            or not set(value) <= {"jsonrpc", "id", "result", "error"}
            or (("result" in value) == ("error" in value))
        ):
            raise error
        if "error" in value:
            detail = value["error"]
            if (
                not isinstance(detail, dict)
                or not {"code", "message"} <= set(detail) <= {"code", "message", "data"}
                or type(detail["code"]) is not int
                or not isinstance(detail["message"], str)
            ):
                raise error
    return value


def client_policy(message: dict, working_directory: str, mcp_servers: tuple[dict, ...]) -> dict:
    """Leave responses and opaque ACP payloads alone; own session creation policy."""
    if "method" not in message:
        return message
    if message["method"] not in ALLOWED_METHODS:
        raise RPCError(-32601, "Method not found", message.get("id"))
    if message["method"] == "session/new":
        params = message.get("params", {})
        if not isinstance(params, dict) or params.get("mcpServers", []) != []:
            raise RPCError(-32602, "Invalid params", message.get("id"))
        message = {**message, "params": {**params, "cwd": working_directory, "mcpServers": list(mcp_servers)}}
    return message


def encode_message(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
