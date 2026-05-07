"""Tests for the GET /campaigns/{id}/budget route (Phase 2.4.4)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(inprocess_client: TestClient) -> TestClient:
    """Reuse the session-scoped TestClient from conftest.py.

    The MCP ``StreamableHTTPSessionManager`` refuses to run its
    lifespan more than once per process, so every in-process test
    must share a single TestClient instance.
    """
    return inprocess_client


@pytest.fixture
def patched_campaigns(monkeypatch: pytest.MonkeyPatch) -> dict:
    campaigns: dict = {
        "c-test": {
            "name": "budget-route-test",
            "runs": [],
            "budget_total_usd": 5.0,
            "budget_used_usd": 1.5,
            "budget_state": "ok",
            "budget_thresholds_emitted": [],
        }
    }

    # The route reads through _campaign_or_404 which calls
    # memory_pkg.load_campaigns under the hood. Patch at both call
    # sites the route can reach (api.routes uses _CAMPAIGNS via
    # load_campaigns, mocked through memory_pkg).
    monkeypatch.setattr("memory_pkg.load_campaigns", lambda: campaigns)
    monkeypatch.setattr("api.routes.load_campaigns", lambda: campaigns, raising=False)
    return campaigns


def test_budget_route_returns_summary(
    client: TestClient,
    patched_campaigns: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import config as _config  # noqa: PLC0415
    monkeypatch.setattr(_config, "BUDGET_ENABLED", True, raising=False)
    monkeypatch.setattr(_config, "BUDGET_THRESHOLDS_PCT", [50, 80, 100], raising=False)

    resp = client.get("/campaigns/c-test/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert body["campaign_id"] == "c-test"
    assert body["enabled"] is True
    assert body["budget_used_usd"] == 1.5
    assert body["budget_total_usd"] == 5.0
    assert body["percentage_used"] == 30.0
    assert body["budget_state"] == "ok"
    assert body["thresholds_emitted"] == []
    assert body["thresholds_pct"] == [50, 80, 100]


def test_budget_route_404_for_unknown(
    client: TestClient, patched_campaigns: dict
) -> None:
    resp = client.get("/campaigns/does-not-exist/budget")
    assert resp.status_code == 404


def test_budget_route_handles_unlimited_total(
    client: TestClient,
    patched_campaigns: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No total set → percentage_used is None, state stays ``ok``."""
    patched_campaigns["c-test"]["budget_total_usd"] = None
    patched_campaigns["c-test"]["budget_used_usd"] = 99.0

    resp = client.get("/campaigns/c-test/budget")
    body = resp.json()
    assert body["budget_total_usd"] is None
    assert body["percentage_used"] is None
    assert body["budget_used_usd"] == 99.0
