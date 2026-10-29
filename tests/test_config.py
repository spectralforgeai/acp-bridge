import json

import pytest

from acp_bridge.config import Settings

TOKEN = "configuration-token-with-at-least-forty-three-ascii-characters"


def configure(monkeypatch):
    monkeypatch.setenv("BRIDGE_DEVICE_TOKEN", TOKEN)
    monkeypatch.setenv("ACP_AGENT_COMMAND", json.dumps(["executable", "literal argument", "--flag"]))
    monkeypatch.setenv("ACP_WORKING_DIRECTORY", "/server-owned/workspace")
    monkeypatch.delenv("ACP_MCP_SERVERS", raising=False)


def test_environment_contains_only_generic_relay_configuration(monkeypatch):
    configure(monkeypatch)
    settings = Settings.from_env()
    assert settings.agent_command == ("executable", "literal argument", "--flag")
    assert settings.working_directory == "/server-owned/workspace"
    assert settings.mcp_servers == ()
    assert settings.max_frame_bytes == 1_048_576
    assert settings.startup_seconds == 20
    assert TOKEN not in repr(settings)
    assert "literal argument" not in repr(settings)


@pytest.mark.parametrize("key, value", [
    ("ACP_AGENT_COMMAND", "not JSON"),
    ("ACP_AGENT_COMMAND", '"executable --not-a-shell"'),
    ("ACP_AGENT_COMMAND", "[]"),
    ("ACP_AGENT_COMMAND", '["executable", 4]'),
    ("ACP_AGENT_COMMAND", '["executable", ""]'),
    ("ACP_AGENT_COMMAND", '["executable", "\\u0000"]'),
    ("ACP_MCP_SERVERS", "not JSON"),
    ("ACP_MCP_SERVERS", "{}"),
    ("ACP_MCP_SERVERS", "[null]"),
    ("ACP_MCP_SERVERS", '[{"name":"x","command":"executable","args":[],"env":{}}]'),
    ("ACP_MCP_SERVERS", '[{"name":"x","command":"executable","args":[4],"env":[]}]'),
    ("ACP_MCP_SERVERS", '[{"name":"x","type":"http","url":"https://tools.invalid","headers":[{"name":"X","value":4}]}]'),
    ("ACP_MCP_SERVERS", '[{"name":"x","type":"other","url":"https://tools.invalid","headers":[]}]'),
    ("ACP_WORKING_DIRECTORY", "relative-workspace"),
    ("ACP_WORKING_DIRECTORY", "/workspace\0private"),
    ("BRIDGE_DEVICE_TOKEN", "too-short"),
    ("BRIDGE_DEVICE_TOKEN", "x" * 44 + " "),
    ("BRIDGE_DEVICE_TOKEN", "é" * 44),
    ("ACP_MAX_FRAME_BYTES", "255"),
    ("ACP_MAX_FRAME_BYTES", "16777217"),
    ("ACP_STARTUP_SECONDS", "nan"),
    ("ACP_IDLE_SECONDS", "inf"),
    ("ACP_SESSION_SECONDS", "0"),
    ("ACP_IO_SECONDS", "-1"),
    ("ACP_TERMINATE_SECONDS", "invalid"),
])
def test_invalid_configuration_fails_without_exposing_values(monkeypatch, key, value):
    configure(monkeypatch)
    # Environment variables cannot contain NUL; this case belongs at the direct
    # settings boundary rather than os.environ.
    if "\0" in value:
        with pytest.raises(RuntimeError, match="^Relay configuration is invalid$"):
            Settings(device_token=TOKEN, agent_command=("executable",), working_directory=value)
    else:
        monkeypatch.setenv(key, value)
        with pytest.raises(RuntimeError, match="^Relay configuration is invalid$"):
            Settings.from_env()


@pytest.mark.parametrize("missing", ["BRIDGE_DEVICE_TOKEN", "ACP_AGENT_COMMAND", "ACP_WORKING_DIRECTORY"])
def test_all_server_owned_configuration_is_required(monkeypatch, missing):
    configure(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match="^Relay configuration is invalid$"):
        Settings.from_env()


@pytest.mark.parametrize("server", [
    {"name": "generic-stdio", "command": "/remote/mcp", "args": ["arg"], "env": []},
    {"name": "generic-http", "type": "http", "url": "https://tools.invalid/mcp", "headers": []},
    {"name": "generic-sse", "type": "sse", "url": "https://tools.invalid/sse", "headers": []},
])
def test_standard_admin_mcp_descriptor_shapes(monkeypatch, server):
    configure(monkeypatch)
    monkeypatch.setenv("ACP_MCP_SERVERS", json.dumps([server]))
    assert Settings.from_env().mcp_servers == (server,)
