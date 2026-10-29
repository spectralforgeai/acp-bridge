"""Exercise the public CLI's credential-free help and fail-closed startup."""

import os
import subprocess
import sys

import pytest


SECRET = "cli-private-sentinel-with-more-than-forty-three-ascii-characters"


def invoke(tmp_path, *args, configuration=None):
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("ACP_") and key != "BRIDGE_DEVICE_TOKEN"
    }
    environment.update(configuration or {})
    return subprocess.run(
        [sys.executable, "-m", "acp_bridge", *args],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_help_is_available_without_credentials(tmp_path):
    result = invoke(tmp_path, "--help")
    assert result.returncode == 0
    assert "--host" in result.stdout
    assert "--port" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("configuration", [
    {},
    {
        "BRIDGE_DEVICE_TOKEN": SECRET,
        "ACP_AGENT_COMMAND": '["' + SECRET + '"',
        "ACP_WORKING_DIRECTORY": "/workspace",
    },
    {
        "BRIDGE_DEVICE_TOKEN": SECRET,
        "ACP_AGENT_COMMAND": '["' + SECRET + '"]',
        "ACP_WORKING_DIRECTORY": SECRET,
    },
])
def test_invalid_startup_configuration_exits_without_exposing_values(tmp_path, configuration):
    result = invoke(tmp_path, configuration=configuration)
    assert result.returncode == 2
    assert result.stdout == ""
    assert SECRET not in result.stdout + result.stderr
