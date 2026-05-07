"""End-to-end smoke test: an external MCP client can discover and call
the orchestrator's tools through the /mcp endpoint (Phase 1.7).

The test runs by spawning a uvicorn subprocess on a free port, pointing
an mcp.ClientSession at it over streamable HTTP, and exercising discovery
and invocation.  Using a subprocess avoids conflicting with the session-
scoped ``inprocess_client`` fixture: the ``StreamableHTTPSessionManager``
singleton can only call ``.run()`` once per instance, so an in-process
uvicorn thread would race with any test that uses ``inprocess_client``.
A subprocess gets its own process + session manager instance.

ASGITransport was evaluated first (as instructed) and found to be
incompatible with MCP SDK v1.27.0's streamable HTTP transport: the
client opens a long-lived SSE connection on the initialise POST and
concurrently POSTs subsequent requests, which requires two independent
HTTP connections.  ASGITransport serialises requests in one async
context and hangs.  See the inline note below for details.

``pytest-asyncio`` is configured with ``asyncio_mode = "auto"``
(pyproject.toml), so ``async def test_*`` functions are collected
automatically.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# ---------------------------------------------------------------------------
# Transport note
# ---------------------------------------------------------------------------
# ASGITransport + streamable_http_client (MCP SDK v1.27.0) is incompatible:
#
# 1.  The MCP client sends a streaming POST to /mcp/mcp and expects a
#     text/event-stream response that stays open (server-sent events).
# 2.  While that SSE channel is live, the client POSTs *additional* requests
#     (list_tools, etc.) over separate HTTP connections.
# 3.  httpx.ASGITransport processes each request inside the running async
#     context and cannot handle two concurrent streaming requests in the same
#     event loop → the initialise POST hangs indefinitely.
#
# Subprocess-uvicorn gives us a real socket, a real event loop in the child,
# and a fresh StreamableHTTPSessionManager instance that does not conflict
# with the session-scoped inprocess_client fixture used by other test files.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The MCP endpoint path: FastMCP mounts at /mcp (its default
# streamable_http_path) and app.py mounts that sub-app at /mcp, so the
# effective path is /mcp/mcp.
_MCP_PATH = "/mcp/mcp"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Session-scoped fixture: start a real uvicorn server in a subprocess.
# Using a subprocess (not a thread) means:
#   - The child has its own process + asyncio event loop.
#   - Its StreamableHTTPSessionManager instance is independent of the one
#     used by inprocess_client in the parent pytest session.
#   - No ordering constraints on test collection.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mcp_base_url() -> str:  # type: ignore[return]
    """Session-scoped URL of a live uvicorn server exposing the /mcp endpoint."""
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": _REPO_ROOT}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=_REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait up to 30 s for the server to be ready.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    else:
        proc.terminate()
        pytest.fail("uvicorn subprocess did not become healthy within 30 s")

    yield f"http://127.0.0.1:{port}"

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Per-test async context manager helper.
#
# Each test opens its own MCP ClientSession within a single async task so
# that anyio cancel-scope setup and teardown always happen in the same task.
# (A shared session-scoped async fixture would require awaiting coroutines
# from the background thread's event loop inside the test's event loop,
# which is not supported and causes cross-loop errors.)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _open_mcp_session(base_url: str) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP ClientSession against the given server URL."""
    url = f"{base_url}{_MCP_PATH}"
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_external_client_initialize(mcp_base_url: str) -> None:
    """initialize() completes successfully (session is alive)."""
    async with _open_mcp_session(mcp_base_url) as session:
        # initialize() was already called inside _open_mcp_session.
        # list_tools() confirms the session is alive.
        tools_result = await session.list_tools()
        assert tools_result is not None


async def test_external_client_lists_all_tools(mcp_base_url: str) -> None:
    """All 9 orchestrator MCP tools are discoverable over the wire."""
    async with _open_mcp_session(mcp_base_url) as session:
        tools_result = await session.list_tools()
    names = {t.name for t in tools_result.tools}
    expected = {
        "orchestrate",
        "get_run_status",
        "get_run_result",
        "list_targets",
        "list_models",
        "run_dream_cycle",
        "add_safety_gate",
        "reload_agents",
        "update_agent_prompt",
    }
    assert names == expected, f"diff: {names ^ expected}"


async def test_external_client_tool_annotations_present(mcp_base_url: str) -> None:
    """Tools advertise the per-tool annotations added in Commit 3."""
    async with _open_mcp_session(mcp_base_url) as session:
        tools_result = await session.list_tools()
    by_name = {t.name: t for t in tools_result.tools}

    orchestrate = by_name["orchestrate"]
    assert orchestrate.annotations is not None
    assert orchestrate.annotations.openWorldHint is True

    list_targets = by_name["list_targets"]
    assert list_targets.annotations is not None
    assert list_targets.annotations.readOnlyHint is True


async def test_external_client_tool_meta_present(mcp_base_url: str) -> None:
    """Tools advertise the orchestrator's freeform meta (category, requires_target)."""
    async with _open_mcp_session(mcp_base_url) as session:
        tools_result = await session.list_tools()
    by_name = {t.name: t for t in tools_result.tools}

    orchestrate_meta = by_name["orchestrate"].meta or {}
    assert orchestrate_meta.get("category") == "orchestration"
    assert orchestrate_meta.get("requires_target") is True

    list_targets_meta = by_name["list_targets"].meta or {}
    assert list_targets_meta.get("category") == "ops"
    assert list_targets_meta.get("requires_target") is False


async def test_external_client_lists_resources(mcp_base_url: str) -> None:
    """All known orchestrator://* resources are discoverable, including the
    Phase 1.7 contract resource."""
    async with _open_mcp_session(mcp_base_url) as session:
        res_result = await session.list_resources()
    uris = {str(r.uri) for r in res_result.resources}
    assert "orchestrator://contract" in uris
    assert "orchestrator://health" in uris
    assert "orchestrator://identity" in uris


async def test_external_client_reads_contract_resource(mcp_base_url: str) -> None:
    """The contract resource round-trips over the wire and is parseable JSON
    with the expected top-level shape."""
    async with _open_mcp_session(mcp_base_url) as session:
        read_result = await session.read_resource("orchestrator://contract")
    contents = read_result.contents
    assert contents, "contract resource returned empty contents"
    text = contents[0].text  # text resource
    payload = json.loads(text)
    assert payload["name"] == "AI Orchestrator"
    assert "version" in payload
    parts = payload["version"].split(".")
    assert len(parts) == 3
    assert {t["name"] for t in payload["tools"]} >= {"orchestrate", "list_targets"}


async def test_external_client_calls_readonly_tool(mcp_base_url: str) -> None:
    """A read-only tool (list_targets) round-trips a structured response."""
    async with _open_mcp_session(mcp_base_url) as session:
        result = await session.call_tool("list_targets", arguments={})
    # MCP CallToolResult: structuredContent + content blocks
    assert result.isError is False or result.isError is None
    # Either structuredContent or text content should carry the targets list.
    if result.structuredContent is not None:
        assert "targets" in result.structuredContent
    else:
        # Fallback: parse the first text block as JSON.
        assert result.content, "no content in call_tool result"
        text = result.content[0].text
        payload = json.loads(text)
        assert "targets" in payload


async def test_external_client_lists_prompts(mcp_base_url: str) -> None:
    """All 3 orchestrator prompts are discoverable."""
    async with _open_mcp_session(mcp_base_url) as session:
        res = await session.list_prompts()
    names = {p.name for p in res.prompts}
    assert names == {"plan_task", "review_code", "troubleshoot_error"}
