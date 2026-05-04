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
