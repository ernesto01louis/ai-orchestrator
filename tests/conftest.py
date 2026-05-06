"""Shared pytest fixtures.

The test suite splits into two flavors:

- `live` tests hit the orchestrator over HTTP at http://127.0.0.1:8000.
  Run them with the service active: `systemctl status ai-orchestrator`.
- `inprocess` tests import `app.py` in this process and exercise it via
  Starlette's TestClient. They never touch the live service.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from prefect.testing.utilities import prefect_test_harness

# Make the orchestrator package importable as if we ran from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True, scope="session")
def _prefect_test_harness():
    """All tests run with a temporary Prefect SQLite DB so @flow/@task
    decorators function (state tracking, hooks fire) without needing
    a real Prefect server."""
    with prefect_test_harness():
        yield


LIVE_BASE_URL = os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="session")
def base_url() -> str:
    return LIVE_BASE_URL


@pytest.fixture(scope="session")
def http(base_url: str) -> httpx.Client:
    """Synchronous httpx client pointed at the live orchestrator."""
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        yield client


def _live_or_skip(http: httpx.Client) -> None:
    try:
        r = http.get("/health", timeout=3.0)
        if r.status_code != 200:
            pytest.skip(f"orchestrator not healthy ({r.status_code})")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"orchestrator unreachable at {http.base_url}: {exc}")


@pytest.fixture(scope="session")
def inprocess_client():
    """Session-scoped Starlette TestClient against the FastAPI app.

    Must be a singleton: the MCP sub-app's StreamableHTTPSessionManager
    refuses to run its lifespan more than once per process, so multiple
    TestClient instances would crash with RuntimeError on the second one.
    """
    from fastapi.testclient import TestClient

    import app  # noqa: WPS433

    with TestClient(app.app) as c:
        yield c


@pytest.fixture
def live(http: httpx.Client) -> httpx.Client:
    """Use this for any test that requires the live orchestrator."""
    _live_or_skip(http)
    return http


@pytest.fixture
def mock_ollama(monkeypatch):
    """Patch llm.ollama.requests.post so agent tasks run without a real Ollama server.

    Returns the patched ``requests.post`` MagicMock so tests can assert on
    ``.call_count``, ``.call_args_list``, etc. The fake response is reachable via
    ``mock_ollama.return_value``.

    Provides a generic envelope adequate for agents that accept a score/files JSON
    shape; agents with stricter schemas (e.g., planner) may need per-test re-patching.
    """
    _CANNED_JSON = '{"score": 0.8, "files": {"main.py": "print(\\"hi\\")"}}'

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "response": _CANNED_JSON,
        "message": {"content": _CANNED_JSON},
        "eval_count": 7,
    }

    post_mock = MagicMock(return_value=fake_response)
    monkeypatch.setattr("llm.ollama.requests.post", post_mock)
    return post_mock
