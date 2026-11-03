import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

_BUNDLED = Path(__file__).resolve().parents[2] / "tools" / "terraform-mcp-server.exe"
logger = logging.getLogger(__name__)
_mcp_process: subprocess.Popen[bytes] | None = None
MCP_URL = "http://127.0.0.1:8080/mcp"


def mcp_status() -> dict[str, object]:
    """Return observable status for the separately managed MCP HTTP service."""
    configured_url = os.getenv("TERRAFORM_MCP_URL", "").strip() or MCP_URL
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", 8080))
            reachable = True
        except OSError:
            reachable = False
    return {
        "url": configured_url,
        "reachable": reachable,
        "managed_by_api": _mcp_process is not None,
        "pid": _mcp_process.pid if _mcp_process is not None and _mcp_process.poll() is None else None,
    }


def start_mcp_server(configured_url: str = "") -> str | None:
    """Start the bundled registry MCP HTTP service when no URL is configured."""
    global _mcp_process
    effective_url = configured_url.strip() or os.getenv("TERRAFORM_MCP_URL", "").strip()
    if effective_url:
        os.environ["TERRAFORM_MCP_URL"] = effective_url
        return effective_url
    if _mcp_process is not None and _mcp_process.poll() is None:
        os.environ["TERRAFORM_MCP_URL"] = MCP_URL
        return MCP_URL
    if not _BUNDLED.is_file():
        logger.warning("Terraform MCP binary is unavailable; using local fallback if possible")
        return None
    try:
        _mcp_process = subprocess.Popen(
            [str(_BUNDLED), "streamable-http", "--transport-host", "127.0.0.1",
             "--transport-port", "8080", "--toolsets", "registry"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        logger.exception("Could not start Terraform MCP HTTP service")
        return None
    os.environ["TERRAFORM_MCP_URL"] = MCP_URL
    logger.info("Terraform MCP service started at %s", MCP_URL)
    return MCP_URL


def stop_mcp_server() -> None:
    global _mcp_process
    if _mcp_process is None:
        return
    if _mcp_process.poll() is None:
        _mcp_process.terminate()
        try:
            _mcp_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _mcp_process.kill()
    _mcp_process = None
    os.environ.pop("TERRAFORM_MCP_URL", None)


async def load_registry_tools(
    mcp_url: str = "",
) -> tuple[list[BaseTool], bool, MultiServerMCPClient | None]:
    """Load public-registry tools from the official Terraform MCP server.

    HCP Terraform and apply tools are not enabled. A missing server returns
    an empty tool list so the chat can still write Terraform files.
    """
    effective_url = mcp_url.strip() or os.getenv("TERRAFORM_MCP_URL", "").strip()
    if not effective_url and mcp_status()["reachable"]:
        effective_url = MCP_URL
    if effective_url:
        connection = {
            "terraform-registry": {
                "url": effective_url,
                "transport": "streamable_http",
            }
        }
    else:
        command = _server_command()
        if command is None:
            return [], False, None
        connection = {
            "terraform-registry": {
                "command": command[0],
                "args": command[1:],
                "transport": "stdio",
            }
        }
    try:
        client = MultiServerMCPClient(connection)
        tools = await client.get_tools()
    except Exception:
        logger.exception("Could not connect to Terraform MCP server")
        return [], False, None
    return tools, bool(tools), client


def _server_command() -> list[str] | None:
    if _BUNDLED.is_file():
        return [str(_BUNDLED), "stdio", "--toolsets=registry"]
    binary = shutil.which("terraform-mcp-server")
    if binary:
        return [binary, "stdio", "--toolsets=registry"]
    return None
