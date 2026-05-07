"""Tests for POST /runs/{id}/burst + companion routes (Phase 2.5.3)."""
from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(inprocess_client: TestClient) -> TestClient:
    return inprocess_client


@pytest.fixture
def known_run() -> Iterator[str]:
    """Inject a fake run into RUN_STATUS so the route's run_id check passes."""
    from core.runtime import RUN_STATUS  # noqa: PLC0415

    rid = "test-burst-run"
    RUN_STATUS[rid] = {"phase": "queued", "score": 0, "completed": False}
    yield rid
    RUN_STATUS.pop(rid, None)


@pytest.fixture
def fake_sky_module(monkeypatch: pytest.MonkeyPatch, tmp_path) -> MagicMock:
    """Replace ``core.sky`` interactions with a controlled fake."""
    from core import config, sky  # noqa: PLC0415

    sky.reset_for_tests()

    yaml_dir = tmp_path / "sky"
    yaml_dir.mkdir()
    (yaml_dir / "llm-burst.yaml").write_text("name: llm")
    monkeypatch.setattr(config, "SKY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "SKY_YAML_DIR", str(yaml_dir), raising=False)
    monkeypatch.setattr(config, "SKY_MAX_BURST_COST_USD", 5.0, raising=False)

    fake_sdk = MagicMock(name="FakeSky")
    fake_sdk.launch.return_value = "request-1"
    # cost_report returns an empty list so the route falls back to
    # the registered estimate — keeps the test deterministic.
    fake_sdk.cost_report.return_value = []
    monkeypatch.setattr(sky, "_sky_module", fake_sdk, raising=False)
    monkeypatch.setattr(sky, "_build_task", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(sky, "_submit_task", lambda *_a, **_k: "req-id")
    return fake_sdk


# ---------------------------------------------------------------------------
# POST /runs/{id}/burst
# ---------------------------------------------------------------------------


def test_burst_404_for_unknown_run(client: TestClient) -> None:
    resp = client.post("/runs/does-not-exist/burst", json={"spec_name": "llm-burst"})
    assert resp.status_code == 404


def test_burst_503_when_sky_disabled(
    client: TestClient,
    known_run: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import config  # noqa: PLC0415
    monkeypatch.setattr(config, "SKY_ENABLED", False, raising=False)
    resp = client.post(
        f"/runs/{known_run}/burst",
        json={"spec_name": "llm-burst", "estimated_cost_usd": 0.1},
    )
    assert resp.status_code == 503


def test_burst_404_for_unknown_spec(
    client: TestClient, known_run: str, fake_sky_module: MagicMock,
) -> None:
    resp = client.post(
        f"/runs/{known_run}/burst",
        json={"spec_name": "nope", "estimated_cost_usd": 0.1},
    )
    assert resp.status_code == 404


def test_burst_422_when_estimate_exceeds_ceiling(
    client: TestClient, known_run: str, fake_sky_module: MagicMock,
) -> None:
    resp = client.post(
        f"/runs/{known_run}/burst",
        json={"spec_name": "llm-burst", "estimated_cost_usd": 99.0},
    )
    assert resp.status_code == 422


def test_burst_422_when_spec_name_missing(
    client: TestClient, known_run: str, fake_sky_module: MagicMock,
) -> None:
    resp = client.post(f"/runs/{known_run}/burst", json={})
    assert resp.status_code == 422


def test_burst_happy_path_returns_handle(
    client: TestClient, known_run: str, fake_sky_module: MagicMock,
) -> None:
    resp = client.post(
        f"/runs/{known_run}/burst",
        json={
            "spec_name": "llm-burst",
            "accelerator": "A100:1",
            "estimated_cost_usd": 1.25,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == known_run
    assert body["spec_name"] == "llm-burst"
    assert body["estimated_cost_usd"] == 1.25
    assert body["cluster_name"].startswith("orch-")


# ---------------------------------------------------------------------------
# GET /runs/{id}/bursts
# ---------------------------------------------------------------------------


def test_list_bursts_filters_by_run_id(
    client: TestClient, known_run: str, fake_sky_module: MagicMock,
) -> None:
    """Launch a burst against ``known_run`` then list it; bursts owned
    by other runs must not appear."""
    client.post(
        f"/runs/{known_run}/burst",
        json={"spec_name": "llm-burst", "estimated_cost_usd": 0.5},
    )
    resp = client.get(f"/runs/{known_run}/bursts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == known_run
    assert len(body["bursts"]) == 1
    assert body["bursts"][0]["spec_name"] == "llm-burst"


# ---------------------------------------------------------------------------
# POST /runs/{id}/bursts/{name}/stop
# ---------------------------------------------------------------------------


def test_stop_burst_returns_actual_cost(
    client: TestClient, known_run: str, fake_sky_module: MagicMock,
) -> None:
    """Stop deregisters the burst, returns the actual cost (falling
    back to the registered estimate when SkyPilot has no cost_report)."""
    launch = client.post(
        f"/runs/{known_run}/burst",
        json={"spec_name": "llm-burst", "estimated_cost_usd": 1.5},
    ).json()
    cluster_name = launch["cluster_name"]

    resp = client.post(f"/runs/{known_run}/bursts/{cluster_name}/stop")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stopped"] is True
    assert body["cluster_name"] == cluster_name
    assert body["actual_cost_usd"] == 1.5

    # Subsequent list should be empty (deregistered).
    list_resp = client.get(f"/runs/{known_run}/bursts").json()
    assert list_resp["bursts"] == []


def test_stop_burst_404_for_unknown_run(client: TestClient) -> None:
    resp = client.post("/runs/nope/bursts/c-1/stop")
    assert resp.status_code == 404
