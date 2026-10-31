import asyncio
import shutil
import subprocess

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

_IMAGE = "hashicorp/terraform-mcp-server:1.3.0"


def load_registry_tools() -> tuple[list[BaseTool], bool]:
    """Load public-registry tools from the official Terraform MCP server.

    HCP Terraform and apply tools are not enabled. A missing server returns
    an empty tool list so the chat can still write Terraform files.
    """
    command = _server_command()
    if command is None:
        return [], False
    connection = {
        "terraform-registry": {
            "command": command[0],
            "args": command[1:],
            "transport": "stdio",
        }
    }
    try:
        tools = asyncio.run(_fetch_tools(connection))
    except Exception:
        return [], False
    return tools, bool(tools)


async def _fetch_tools(connection: dict) -> list[BaseTool]:
    client = MultiServerMCPClient(connection)
    return await client.get_tools()


def _server_command() -> list[str] | None:
    if _docker_ready():
        return ["docker", "run", "-i", "--rm", _IMAGE, "--toolsets=registry"]
    binary = shutil.which("terraform-mcp-server")
    if binary:
        return [binary, "stdio", "--toolsets=registry"]
    return None


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
