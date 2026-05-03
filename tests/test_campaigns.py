"""Campaign lifecycle and grid-expansion tests (Phase 1.1).

In-process via TestClient so we do not require a running orchestrator
service. Patches the campaign runner to a no-op so POST /campaigns does
not actually drive Ollama-backed runs during the test.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.inprocess


# ── unit tests: no service, no FastAPI ─────────────


def test_expand_grid_full_product():
    from orchestration.campaign import expand_grid

    combos = expand_grid({"a": [1, 2], "b": ["x", "y"]})
    assert len(combos) == 4
    assert {"a": 1, "b": "x"} in combos
    assert {"a": 2, "b": "y"} in combos


def test_expand_grid_max_runs_truncates():
    from orchestration.campaign import expand_grid

    combos = expand_grid({"a": [1, 2, 3], "b": ["x", "y"]}, max_runs=3)
    assert len(combos) == 3


def test_expand_grid_empty_yields_single_combo():
    """No params → one run with no per-combo overrides."""
    from orchestration.campaign import expand_grid

    assert expand_grid({}) == [{}]


def test_materialize_template_substitutes_string_fields():
    from orchestration.campaign import materialize_template

    out = materialize_template(
        {
            "project_name": "sweep_{seed}",
            "prompt": "constant",
            "generator_models": ["m1", "m2_{tag}"],
            "max_iterations": 5,
        },
        {"seed": 42, "tag": "beta"},
    )
    assert out["project_name"] == "sweep_42"
    assert out["prompt"] == "constant"
    assert out["generator_models"] == ["m1", "m2_beta"]
    assert out["max_iterations"] == 5


# ── lifecycle tests via TestClient ─────────────────


@pytest.fixture
def client(inprocess_client, monkeypatch):
    """TestClient (session-scoped, shared across all in-process tests so
    Starlette's MCP sub-app lifespan only fires once) with run_campaign
    stubbed to a no-op so created campaigns sit in queued state without
    consuming Ollama.
    """
    import orchestration.campaign as oc

    def _noop(_id):
        time.sleep(0.5)

    monkeypatch.setattr(oc, "run_campaign", _noop)
    return inprocess_client


@pytest.fixture
def cleanup_campaigns():
    """Remove campaigns created during a test from durable state."""
    created: list[str] = []
    yield created
    if not created:
        return
    from memory_pkg import load_campaigns, save_campaigns

    campaigns = load_campaigns()
    for cid in created:
        campaigns.pop(cid, None)
    save_campaigns(campaigns)


def _first_target() -> str:
    """First configured SSH target — works against both the local LXC
    (real targets) and CI (config.example.json has 'example-target')."""
    from core.config import SSH_TARGETS
    return next(iter(SSH_TARGETS.keys()))


_MIN_TEMPLATE = {
    "project_name": "test_campaign_smoke",
    "prompt": "noop",
    "planner_model": "qwen2.5-coder:32b",
    "generator_models": ["qwen2.5-coder:32b"],
    "judge_model": "qwen2.5-coder:32b",
    "deploy_target": _first_target(),
}


def test_campaigns_create_and_list(client, cleanup_campaigns):
    body = {"name": "smoke-create", "hypothesis": "h", "template": _MIN_TEMPLATE, "params": {"seed": [1, 2]}}
    r = client.post("/campaigns", json=body)
    assert r.status_code == 200, r.text
    cid = r.json()["campaign_id"]
    cleanup_campaigns.append(cid)
    assert r.json()["run_count"] == 2
    assert r.json()["status"] == "started"

    r = client.get("/campaigns")
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["campaigns"]]
    assert cid in ids


def test_campaigns_get_returns_full_record(client, cleanup_campaigns):
    body = {"name": "smoke-get", "hypothesis": "h", "template": _MIN_TEMPLATE, "params": {}}
    cid = client.post("/campaigns", json=body).json()["campaign_id"]
    cleanup_campaigns.append(cid)

    r = client.get(f"/campaigns/{cid}")
    assert r.status_code == 200
    record = r.json()
    assert record["id"] == cid
    assert record["name"] == "smoke-get"
    assert record["template"]["project_name"] == _MIN_TEMPLATE["project_name"]


def test_campaigns_404_on_unknown(client):
    r = client.get("/campaigns/nope")
    assert r.status_code == 404


def test_campaigns_reject_missing_hypothesis(client):
    """REFORMS §1 pre-registration: campaigns without a hypothesis are 422."""
    body = {"name": "no-hypothesis", "template": _MIN_TEMPLATE, "params": {}}
    r = client.post("/campaigns", json=body)
    assert r.status_code == 422
    assert "hypothesis" in r.text.lower()


def test_campaigns_reject_blank_hypothesis(client):
    body = {
        "name": "blank-hypothesis", "hypothesis": "   ",
        "template": _MIN_TEMPLATE, "params": {},
    }
    r = client.post("/campaigns", json=body)
    assert r.status_code == 422
    assert "hypothesis" in r.text.lower()


def test_campaigns_pause_resume(client, cleanup_campaigns):
    body = {"name": "smoke-pause", "hypothesis": "h", "template": _MIN_TEMPLATE, "params": {"seed": [1]}}
    cid = client.post("/campaigns", json=body).json()["campaign_id"]
    cleanup_campaigns.append(cid)

    r = client.post(f"/campaigns/{cid}/pause")
    assert r.status_code == 200
    assert r.json()["paused"] is True

    from core.runtime import CAMPAIGN_STATUS

    assert CAMPAIGN_STATUS[cid]["paused"] is True

    r = client.post(f"/campaigns/{cid}/resume")
    assert r.status_code == 200
    assert r.json()["paused"] is False
    assert CAMPAIGN_STATUS[cid]["paused"] is False


def test_campaigns_abort(client, cleanup_campaigns):
    body = {"name": "smoke-abort", "hypothesis": "h", "template": _MIN_TEMPLATE, "params": {"seed": [1]}}
    cid = client.post("/campaigns", json=body).json()["campaign_id"]
    cleanup_campaigns.append(cid)

    r = client.post(f"/campaigns/{cid}/abort")
    assert r.status_code == 200
    assert r.json()["aborted"] is True

    from core.runtime import CAMPAIGN_STATUS

    assert CAMPAIGN_STATUS[cid]["aborted"] is True


def test_campaigns_tree_shape(client, cleanup_campaigns):
    body = {"name": "smoke-tree", "hypothesis": "h", "template": _MIN_TEMPLATE, "params": {"seed": [1, 2]}}
    cid = client.post("/campaigns", json=body).json()["campaign_id"]
    cleanup_campaigns.append(cid)

    r = client.get(f"/campaigns/{cid}/tree")
    assert r.status_code == 200
    body = r.json()
    assert "campaign" in body
    assert "runs" in body
    assert isinstance(body["runs"], list)
    # Stubbed runner doesn't append runs, so the list is empty — that's OK,
    # we are testing route shape, not real execution.


def test_vault_campaign_note_emission(tmp_path, monkeypatch):
    """vault_write_campaign_note writes a file that exists on disk.

    Phase 1.2 will replace this with signed-evidence-bundle assertions;
    for 1.1 we only verify the note lands.
    """
    import memory_pkg as mp

    monkeypatch.setattr(mp, "VAULT_ENABLED", True)
    monkeypatch.setattr(mp, "VAULT_LOCAL_DIR", str(tmp_path))
    # vault_sync_file may try to rsync; make it a no-op.
    monkeypatch.setattr(mp, "vault_sync_file", lambda *a, **kw: None)

    fake = {
        "id": "11111111-2222-3333-4444-555555555555",
        "name": "vault-emit",
        "description": "tiny test",
        "status": "completed",
        "template": {
            "project_name": "p",
            "deploy_target": "localhost",
            "planner_model": "pl",
            "generator_models": ["g"],
            "judge_model": "j",
        },
        "params": {"seed": [1, 2]},
        "max_runs": None,
        "created_at": "2026-04-30T17:00:00",
        "updated_at": "2026-04-30T17:01:00",
        "completed_at": "2026-04-30T17:02:00",
        "runs": [
            {"run_id": "abc", "params": {"seed": 1}, "status": "completed", "score": 7.5},
        ],
    }
    fname = mp.vault_write_campaign_note(fake)
    assert fname is not None
    written = tmp_path / "campaigns" / fname
    assert written.exists()
    text = written.read_text()
    assert "campaign_id: 11111111-2222-3333-4444-555555555555" in text
    assert "vault-emit" in text
