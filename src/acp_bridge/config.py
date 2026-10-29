"""Server-owned ACP command and transport limits; never supplied by clients."""

import json
import math
import os
from dataclasses import dataclass, field

def valid_string(value: object) -> bool:
    return isinstance(value, str) and "\0" not in value


def valid_pairs(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(pair, dict) and set(pair) == {"name", "value"}
        and valid_string(pair["name"]) and bool(pair["name"])
        and valid_string(pair["value"])
        for pair in value
    )


def valid_mcp_server(server: object) -> bool:
    """Validate only standard descriptor structure, never integrated systems."""
    if not isinstance(server, dict) or not valid_string(server.get("name")) or not server["name"]:
        return False
    if "_meta" in server and server["_meta"] is not None and not isinstance(server["_meta"], dict):
        return False
    if server.get("type") in {"http", "sse"}:
        return (
            set(server) <= {"type", "name", "url", "headers", "_meta"}
            and valid_string(server.get("url")) and bool(server["url"])
            and valid_pairs(server.get("headers"))
        )
    return (
        set(server) <= {"name", "command", "args", "env", "_meta"}
        and valid_string(server.get("command")) and bool(server["command"])
        and isinstance(server.get("args"), list)
        and all(valid_string(arg) for arg in server["args"])
        and valid_pairs(server.get("env"))
    )



@dataclass(frozen=True)
class Settings:
    device_token: str = field(repr=False)
    agent_command: tuple[str, ...] = field(repr=False)
    working_directory: str
    mcp_servers: tuple[dict, ...] = field(default=(), repr=False)
    max_frame_bytes: int = 1_048_576
    startup_seconds: float = 20.0
    idle_seconds: float = 300.0
    session_seconds: float = 3600.0
    io_seconds: float = 10.0
    terminate_seconds: float = 0.5

    def __post_init__(self) -> None:
        try:
            if (
                not isinstance(self.device_token, str)
                or len(self.device_token) < 43
                or not self.device_token.isascii()
                or any(c.isspace() for c in self.device_token)
                or not isinstance(self.agent_command, tuple)
                or not self.agent_command
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in self.agent_command)
                or not isinstance(self.working_directory, str)
                or not self.working_directory.startswith("/")
                or "\0" in self.working_directory
                or type(self.max_frame_bytes) is not int
                or not 256 <= self.max_frame_bytes <= 16_777_216
            ):
                raise ValueError
            if not isinstance(self.mcp_servers, tuple) or not all(valid_mcp_server(item) for item in self.mcp_servers):
                raise ValueError
            encoded = json.dumps(self.mcp_servers, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > self.max_frame_bytes:
                raise ValueError
            for value in (
                self.startup_seconds, self.idle_seconds, self.session_seconds,
                self.io_seconds, self.terminate_seconds,
            ):
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError
        except (TypeError, ValueError, RecursionError):
            raise RuntimeError("Relay configuration is invalid") from None

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            argv = json.loads(os.environ["ACP_AGENT_COMMAND"])
            servers = json.loads(os.getenv("ACP_MCP_SERVERS", "[]"))
            if not isinstance(argv, list) or not isinstance(servers, list):
                raise ValueError
            return cls(
                device_token=os.environ["BRIDGE_DEVICE_TOKEN"],
                agent_command=tuple(argv),
                working_directory=os.environ["ACP_WORKING_DIRECTORY"],
                mcp_servers=tuple(servers),
                max_frame_bytes=int(os.getenv("ACP_MAX_FRAME_BYTES", "1048576")),
                startup_seconds=float(os.getenv("ACP_STARTUP_SECONDS", "20")),
                idle_seconds=float(os.getenv("ACP_IDLE_SECONDS", "300")),
                session_seconds=float(os.getenv("ACP_SESSION_SECONDS", "3600")),
                io_seconds=float(os.getenv("ACP_IO_SECONDS", "10")),
                terminate_seconds=float(os.getenv("ACP_TERMINATE_SECONDS", "0.5")),
            )
        except (KeyError, TypeError, ValueError, RecursionError):
            raise RuntimeError("Relay configuration is invalid") from None
