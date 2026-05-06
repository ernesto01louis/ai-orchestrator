"""Tests for Phase 1.5 Phase D: manifest verification HTTP endpoints.

Covers:
- GET /status/{run_id} includes manifest_status (lazy verify for completed runs).
- GET /runs/{run_id}/verify — forces integrity check, updates RUN_STATUS.
- GET /campaigns/{campaign_id}/verify-merkle — re-validates Merkle root, updates CAMPAIGN_STATUS.

Uses the session-scoped inprocess_client fixture from conftest.py (TestClient
backed by the real FastAPI app). No prefect_real marker — runs in default suite.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.runtime import CAMPAIGN_STATUS, RUN_STATUS, _campaign_status_lock, _init_run_status
from manifest import write_campaign_merkle, write_run_manifest

# ---------------------------------------------------------------------------
# Unique IDs to avoid collisions with other tests
# ---------------------------------------------------------------------------

_RUN_PREFIX = "verify-route-test"
_CAMP_PREFIX = "verify-camp-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_status():
    """Wipe RUN_STATUS and CAMPAIGN_STATUS before and after each test.

    Also removes any test campaigns written to the persistent store so
    production campaigns.json is not polluted between test runs.
    """
    from memory_pkg import load_campaigns, save_campaigns

    # Record existing campaign IDs so we only clean up ones we added.
    pre_existing = set(load_campaigns().keys())

    RUN_STATUS.clear()
    with _campaign_status_lock:
        CAMPAIGN_STATUS.clear()
    yield
    RUN_STATUS.clear()
    with _campaign_status_lock:
        CAMPAIGN_STATUS.clear()

    # Remove any campaigns added during this test from the persistent store.
    campaigns = load_campaigns()
    for cid in list(campaigns.keys()):
        if cid not in pre_existing:
            campaigns.pop(cid, None)
    save_campaigns(campaigns)


@pytest.fixture
def client(inprocess_client):
    return inprocess_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_dir(tmp_path: Path, run_id: str, project: str = "test-proj") -> Path:
    """Create a minimal run directory with one artifact file and a manifest.json."""
    run_dir = tmp_path / project / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "score.txt").write_text("9\n")
    (run_dir / "plan.json").write_text(json.dumps({"language": "python"}))
    write_run_manifest(run_dir, run_id=run_id)
    return run_dir


def _register_run(run_id: str, project: str, *, manifest_status=None, completed: bool = True):
    """Populate RUN_STATUS for a run without going through orchestration."""
    _init_run_status(run_id, project=project, completed=completed)
    RUN_STATUS[run_id]["manifest_status"] = manifest_status


def _register_campaign(campaign_id: str, *, manifest_status=None):
    """Populate CAMPAIGN_STATUS and the persistent campaigns store minimally."""
    from memory_pkg import load_campaigns, save_campaigns

    with _campaign_status_lock:
        CAMPAIGN_STATUS[campaign_id] = {
            "phase": "done",
            "paused": False,
            "aborted": False,
            "current_run_id": None,
            "manifest_status": manifest_status,
        }
    campaigns = load_campaigns()
    campaigns[campaign_id] = {
        "name": f"test-{campaign_id}",
        "hypothesis": "test",
        "runs": [],
    }
    save_campaigns(campaigns)


def _cleanup_campaign(campaign_id: str):
    """Remove a test campaign from the persistent store."""
    from memory_pkg import load_campaigns, save_campaigns

    campaigns = load_campaigns()
    campaigns.pop(campaign_id, None)
    save_campaigns(campaigns)


# ---------------------------------------------------------------------------
# /status/{run_id} — manifest_status field
# ---------------------------------------------------------------------------


def test_status_includes_manifest_status_when_completed(tmp_path, client, monkeypatch):
    """GET /status/{run_id} returns manifest_status='ok' for a run with a good manifest."""
    run_id = f"{_RUN_PREFIX}-1"
    project = "smoke"
    _make_run_dir(tmp_path, run_id, project=project)

    # Register a completed run with manifest_status already set (hot path).
    _register_run(run_id, project, manifest_status="ok")
    monkeypatch.setattr("api.routes.PROJECTS_DIR", str(tmp_path))

    resp = client.get(f"/status/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "manifest_status" in body
    assert body["manifest_status"] == "ok"


def test_status_lazy_verify_when_unset(tmp_path, client, monkeypatch):
    """When manifest_status is None on a completed run, GET /status triggers lazy verify
    and caches the result in RUN_STATUS."""
    run_id = f"{_RUN_PREFIX}-2"
    project = "lazy-proj"
    _make_run_dir(tmp_path, run_id, project=project)

    # Completed run with manifest_status=None (simulate pre-Phase-D run).
    _register_run(run_id, project, manifest_status=None, completed=True)
    monkeypatch.setattr("api.routes.PROJECTS_DIR", str(tmp_path))

    resp = client.get(f"/status/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["manifest_status"] == "ok", f"expected 'ok', got {body['manifest_status']!r}"

    # Must be cached so subsequent polls skip recompute.
    assert RUN_STATUS[run_id]["manifest_status"] == "ok"


def test_status_manifest_status_none_when_not_completed(client):
    """GET /status/{run_id} returns manifest_status: null for an in-flight run."""
    run_id = f"{_RUN_PREFIX}-3"
    _init_run_status(run_id, project="running-proj", completed=False)
    RUN_STATUS[run_id]["manifest_status"] = None

    resp = client.get(f"/status/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["manifest_status"] is None


def test_status_404_unknown_run(client):
    resp = client.get("/status/nonexistent-run-uuid")
    assert resp.status_code == 404
    assert "detail" in resp.json()
    assert "nonexistent-run-uuid" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/verify
# ---------------------------------------------------------------------------


def test_run_verify_endpoint_ok(tmp_path, client, monkeypatch):
    """GET /runs/{run_id}/verify returns valid=true, status='ok' for intact manifest."""
    run_id = f"{_RUN_PREFIX}-4"
    project = "verify-proj"
    _make_run_dir(tmp_path, run_id, project=project)
    _register_run(run_id, project, manifest_status=None)
    monkeypatch.setattr("api.routes.PROJECTS_DIR", str(tmp_path))

    resp = client.get(f"/runs/{run_id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["valid"] is True
    assert body["status"] == "ok"
    assert body["mismatches"] == []


def test_run_verify_endpoint_corrupted(tmp_path, client, monkeypatch):
    """GET /runs/{run_id}/verify returns valid=false, status='corrupted' after tampering."""
    run_id = f"{_RUN_PREFIX}-5"
    project = "corrupt-proj"
    run_dir = _make_run_dir(tmp_path, run_id, project=project)
    _register_run(run_id, project, manifest_status=None)
    monkeypatch.setattr("api.routes.PROJECTS_DIR", str(tmp_path))

    # Tamper with a tracked file after manifest was written.
    (run_dir / "score.txt").write_text("TAMPERED\n")

    resp = client.get(f"/runs/{run_id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["status"] == "corrupted"
    assert len(body["mismatches"]) > 0
    # The tampered file should appear in mismatches.
    assert any("score.txt" in m for m in body["mismatches"])

    # RUN_STATUS must be updated.
    assert RUN_STATUS[run_id]["manifest_status"] == "corrupted"


def test_run_verify_endpoint_404_unknown(client):
    """GET /runs/{nonexistent}/verify returns 404."""
    resp = client.get("/runs/nonexistent-uuid/verify")
    assert resp.status_code == 404
    assert "detail" in resp.json()
    assert "nonexistent-uuid" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /campaigns/{campaign_id}/verify-merkle
# ---------------------------------------------------------------------------


def _make_campaign_with_merkle(
    tmp_path: Path, campaign_id: str, project: str = "camp-proj"
) -> tuple[Path, Path]:
    """Create two run dirs with manifests, write a merkle.json, return (campaign_dir, projects_root)."""
    projects_root = tmp_path / "projects"
    campaign_dir = tmp_path / "campaigns" / campaign_id
    campaign_dir.mkdir(parents=True, exist_ok=True)

    run_ids = [f"{campaign_id}-run-a", f"{campaign_id}-run-b"]
    run_tuples = []
    for rid in run_ids:
        run_dir = projects_root / project / "runs" / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "score.txt").write_text("8\n")
        write_run_manifest(run_dir, run_id=rid)
        run_tuples.append((rid, project, run_dir))

    write_campaign_merkle(campaign_dir, run_tuples, campaign_id=campaign_id)
    return campaign_dir, projects_root


def test_campaign_verify_merkle_endpoint_ok(tmp_path, client, monkeypatch):
    """GET /campaigns/{id}/verify-merkle returns valid=true, status='ok' for intact merkle."""
    campaign_id = f"{_CAMP_PREFIX}-1"
    campaign_dir, projects_root = _make_campaign_with_merkle(tmp_path, campaign_id)

    _register_campaign(campaign_id)

    # Redirect the path constants used by the route handler.
    monkeypatch.setattr("api.routes.PROJECTS_DIR", str(projects_root))
    monkeypatch.setattr("api.routes.CAMPAIGN_TEMPLATES_DIR", tmp_path / "campaigns")

    resp = client.get(f"/campaigns/{campaign_id}/verify-merkle")

    assert resp.status_code == 200
    body = resp.json()
    assert body["campaign_id"] == campaign_id
    assert body["valid"] is True
    assert body["status"] == "ok"
    assert body["mismatches"] == []


def test_campaign_verify_merkle_endpoint_corrupted(tmp_path, client, monkeypatch):
    """GET /campaigns/{id}/verify-merkle returns valid=false, status='corrupted' after tampering."""
    campaign_id = f"{_CAMP_PREFIX}-2"
    project = "camp-proj"
    campaign_dir, projects_root = _make_campaign_with_merkle(
        tmp_path, campaign_id, project=project
    )
    _register_campaign(campaign_id)

    # Tamper with one run's manifest.json after merkle was written.
    run_id_a = f"{campaign_id}-run-a"
    manifest_path = projects_root / project / "runs" / run_id_a / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\x00")

    monkeypatch.setattr("api.routes.PROJECTS_DIR", str(projects_root))
    monkeypatch.setattr("api.routes.CAMPAIGN_TEMPLATES_DIR", tmp_path / "campaigns")

    resp = client.get(f"/campaigns/{campaign_id}/verify-merkle")

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["status"] == "corrupted"

    # CAMPAIGN_STATUS must be updated.
    with _campaign_status_lock:
        assert CAMPAIGN_STATUS[campaign_id]["manifest_status"] == "corrupted"


def test_campaign_verify_merkle_endpoint_404_unknown(client):
    """GET /campaigns/{nonexistent}/verify-merkle returns 404."""
    resp = client.get("/campaigns/nonexistent/verify-merkle")
    assert resp.status_code == 404
    assert "detail" in resp.json()
    assert "nonexistent" in resp.json()["detail"]
