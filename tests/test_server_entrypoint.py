"""Startup regression checks for both supported execution styles and MCP stdio."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "liminal_bridge" / "server.py"


def launch_env(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "SWARM_KEY": "bootstrap-test-key",
            "LIMINAL_DB": str(tmp_path / "bridge.db"),
            "LIMINAL_COLLAB_DB": str(tmp_path / "collaboration.db"),
            "LIMINAL_IDENTITY": str(tmp_path / "identity.pem"),
            "PYTHONPATH": str(ROOT),
        }
    )
    return env


@pytest.mark.parametrize("style", ["package", "direct-script"])
def test_cli_startup_from_external_working_directory(tmp_path, style):
    if style == "package":
        args = ["-m", "src.liminal_bridge.server", "--help"]
    else:
        args = [str(SCRIPT), "--help"]
    result = subprocess.run(
        [sys.executable, *args],
        cwd=tmp_path,
        env=launch_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--mode" in result.stdout


@pytest.mark.asyncio
async def test_mcp_client_can_initialize_and_list_tools(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.liminal_bridge.server", "--mode=mcp"],
        env=launch_env(tmp_path),
    )
    async with asyncio.timeout(20):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                assert any(tool.name == "get_pending_commands" for tool in result.tools)


def test_dependency_pin_stays_with_supported_mcp_api():
    requirements = (ROOT / "requirements.txt").read_text()
    assert "mcp==1.30.0" in requirements
