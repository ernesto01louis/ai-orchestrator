"""Phase 2.6 — operator-console contract tests.

Verifies that the backend additions made for the UI bridge are present:

- ``GET /health`` returns the new ``services`` flat dict + ``uptime_s`` + ``version``
  while keeping the legacy nested fields intact.
- ``GET /metrics.json`` returns a Metrics-shaped dict with all expected keys.
- ``GET /runs`` rows include the Phase 2.6 enrichment fields (id, campaign_id,
  model, started_at, paused, hitl_mode, confidence).
- ``GET /status/{run_id}`` returns the same enrichment.
- ``GET /runs/{run_id}/manifest/verify`` is an alias that 404s on unknown run.
- ``POST /runs/{id}/intervene`` accepts the wider action set
  {approve, reject, edit, skip, abort} AND accepts ``payload`` as alias for ``prompt``.
- ``log()`` broadcast envelopes include ``ts``.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture
def client():
    return TestClient(app)


# ── GET /health ─────────────────────────────────────────────────────────


def test_health_includes_phase2_6_fields(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    data = r.json()

    # Phase 2.6 additions
    assert "services" in data
    assert "uptime_s" in data
    assert "version" in data

    services = data["services"]
    expected = {"orchestrator", "ollama", "hindsight", "postgres", "redis",
                "tempo", "prometheus", "dvc"}
    assert set(services.keys()) == expected
    # All values must be one of the three states
    for k, v in services.items():
        assert v in ("ok", "degraded", "down"), f"{k}={v!r}"

    assert isinstance(data["uptime_s"], int)
    assert data["uptime_s"] >= 0
    assert isinstance(data["version"], str) and data["version"]

    # Legacy nested fields retained (Python SDK consumes these)
    assert "orchestrator" in data
    assert "ollama_servers" in data
    assert "active_runs" in data


# ── GET /metrics.json ───────────────────────────────────────────────────


def test_metrics_json_shape(client):
    r = client.get("/metrics.json")
    assert r.status_code == 200, r.text
    data = r.json()

    expected = {
        "llm_calls_total", "llm_calls_rate_5m",
        "llm_tokens_in_total", "llm_tokens_out_total",
        "llm_p50_ms", "llm_p95_ms", "llm_p99_ms",
        "campaigns_active", "runs_active", "runs_paused",
        "budget_total_usd", "budget_used_usd",
        "ollama_queue_depth", "ollama_gpu_util",
        "ollama_vram_used_gb", "ollama_vram_total_gb",
    }
    assert set(data.keys()) == expected

    # All values must be numeric (int or float)
    for k, v in data.items():
        assert isinstance(v, (int, float)), f"{k}={v!r}"


# ── GET /runs enrichment ────────────────────────────────────────────────


def test_runs_rows_include_phase2_6_fields(client):
    r = client.get("/runs")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "runs" in payload

    # If there are any rows, each one must have the Phase 2.6 fields.
    for row in payload["runs"]:
        for field in ("id", "campaign_id", "model", "started_at",
                      "paused", "hitl_mode", "confidence"):
            assert field in row, f"missing {field} in {row}"
        # id mirrors run_id
        assert row["id"] == row["run_id"]
        # hitl_mode is one of the five backend values (Phase 3.1)
        assert row["hitl_mode"] in (
            "full_auto", "gate_only", "checkpoint", "step_by_step", "co_pilot"
        )


# ── GET /status/{id} enrichment ─────────────────────────────────────────


def test_status_unknown_run_404s(client):
    r = client.get("/status/definitely_does_not_exist")
    assert r.status_code == 404


def test_status_known_run_includes_phase2_6_fields(client):
    # Seed a fake run directly into RUN_STATUS so we don't depend on
    # a live orchestrate cycle.
    from core.runtime import RUN_STATUS, _init_run_status

    rid = "test_phase2_6_status_run"
    try:
        _init_run_status(rid, project="ut", target="local",
                         campaign_id="ut-campaign", model="qwen2.5:14b",
                         hitl_mode="checkpoint", paused="hitl:post_planner",
                         smartpause_confidence=0.42)
        r = client.get(f"/status/{rid}")
        assert r.status_code == 200, r.text
        data = r.json()
        # Phase 2.6 fields all present
        assert data["id"] == rid
        assert data["campaign_id"] == "ut-campaign"
        assert data["model"] == "qwen2.5:14b"
        assert data["hitl_mode"] == "checkpoint"
        assert data["paused"] == "hitl:post_planner"
        assert data["confidence"] == 0.42
        assert data["started_at"]  # populated by _init_run_status default
    finally:
        RUN_STATUS.pop(rid, None)


# ── /runs/{id}/manifest/verify alias ────────────────────────────────────


def test_manifest_verify_alias_404s_for_unknown_run(client):
    r = client.get("/runs/unknown_run_xyz/manifest/verify")
    assert r.status_code == 404


# ── POST /runs/{id}/intervene ────────────────────────────────────────────


def test_intervene_accepts_payload_alias_for_prompt(client):
    """The frontend's IntervenePayload uses `payload`, backend used `prompt`.

    Phase 2.6 makes them aliases. Test by sending an `edit` action with
    `payload` only — should pass body validation (will 404 since the
    run_id is fictional, but that's downstream of the alias logic).
    """
    r = client.post("/runs/test_alias_xyz/intervene",
                    json={"action": "edit", "payload": "replacement prompt"})
    # 404 is correct — run_id doesn't exist. Crucially NOT 400 ("requires
    # non-empty 'prompt' field") which would mean the alias didn't trigger.
    assert r.status_code == 404, r.text


def test_intervene_rejects_edit_without_prompt_or_payload(client):
    """Edit action with neither prompt nor payload should 400."""
    from core.runtime import RUN_STATUS, _init_run_status
    rid = "test_intervene_bad_edit"
    try:
        _init_run_status(rid, project="ut", target="local")
        r = client.post(f"/runs/{rid}/intervene", json={"action": "edit"})
        assert r.status_code == 400
        assert "prompt" in r.json()["detail"] or "payload" in r.json()["detail"]
    finally:
        RUN_STATUS.pop(rid, None)


def test_intervene_accepts_skip_and_abort_actions(client):
    """Phase 2.6 widens the action set to include skip and abort."""
    from core.runtime import RUN_STATUS, _init_run_status
    rid = "test_intervene_skip_abort"
    try:
        _init_run_status(rid, project="ut", target="local")
        for action in ("skip", "abort"):
            r = client.post(f"/runs/{rid}/intervene", json={"action": action})
            # 200 means the action was accepted and queued (not 400 = unknown
            # action). 409 means queue full but still recognized.
            assert r.status_code in (200, 409), f"action={action} got {r.status_code}: {r.text}"
    finally:
        RUN_STATUS.pop(rid, None)


def test_intervene_rejects_unknown_action(client):
    from core.runtime import RUN_STATUS, _init_run_status
    rid = "test_intervene_bad_action"
    try:
        _init_run_status(rid, project="ut", target="local")
        r = client.post(f"/runs/{rid}/intervene", json={"action": "nope"})
        assert r.status_code == 400
    finally:
        RUN_STATUS.pop(rid, None)


# ── /campaigns enrichment ────────────────────────────────────────────────


def test_campaigns_rows_include_phase2_6_fields(client):
    r = client.get("/campaigns")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "campaigns" in payload
    for row in payload["campaigns"][:5]:  # sample first few
        for field in ("hitl_mode", "children", "completed", "failed",
                      "started_at", "budget", "grid"):
            assert field in row, f"missing {field}"
        budget = row["budget"]
        for bk in ("used", "total", "percentage", "state"):
            assert bk in budget
        assert budget["state"] in ("healthy", "warn", "err")


# ── WS log envelope includes ts ──────────────────────────────────────────


def test_log_broadcast_envelope_includes_ts(monkeypatch):
    """Capture _ws_broadcast invocations to confirm log envelopes carry ts."""
    from core import runtime

    captured: list[dict] = []

    def fake_broadcast(msg):
        captured.append(msg)

    monkeypatch.setattr(runtime, "_ws_broadcast", fake_broadcast)

    rid = "test_log_ts_run"
    try:
        runtime._init_run_status(rid, project="ut", target="local")
        runtime.log(rid, "test phase message")
        log_envelopes = [m for m in captured if m.get("type") == "log"]
        assert log_envelopes, "no log envelope captured"
        for env in log_envelopes:
            assert "ts" in env, f"log envelope missing ts: {env}"
            # Round-trip parseability — ISO-8601 with Z suffix
            assert env["ts"].endswith("Z")
            assert "T" in env["ts"]
    finally:
        runtime.RUN_STATUS.pop(rid, None)


# ── frontend-shape happy path (smoke check) ──────────────────────────────


def test_runs_shape_round_trips_to_json(client):
    """All Phase 2.6 fields must be JSON-serializable so the UI can decode."""
    r = client.get("/runs")
    assert r.status_code == 200
    # If json.dumps would have raised in the handler it would have 500'd —
    # belt-and-suspenders: re-encode here.
    json.dumps(r.json())


# ── SPA static mount ────────────────────────────────────────────────────


def test_console_spa_root_serves_index_html(client):
    """The /console mount must serve the SPA index when the build exists."""
    from pathlib import Path
    dist = Path("/opt/ai-orchestrator/ui/console/dist")
    if not dist.is_dir():
        pytest.skip("SPA build missing — run `cd ui/console && npm run build`")

    r = client.get("/console/")
    assert r.status_code == 200
    body = r.text
    assert "<div id=\"root\"" in body or "id=\"root\"" in body


def test_console_spa_deep_link_falls_back_to_index(client):
    """react-router client-side routes must serve index.html, not 404."""
    from pathlib import Path
    if not Path("/opt/ai-orchestrator/ui/console/dist").is_dir():
        pytest.skip("SPA build missing")

    for deep in ("/console/dashboard", "/console/runs/abc",
                 "/console/hitl", "/console/campaigns"):
        r = client.get(deep)
        assert r.status_code == 200, f"{deep} → {r.status_code}"
        assert "id=\"root\"" in r.text


def test_console_prefix_bypasses_bearer_auth():
    """Verify the auth middleware lets /console/* through even with a token set.

    Surgical: instantiates the middleware directly rather than reloading
    the app, so this test is isolated from session-wide module state.
    """
    import asyncio
    from core.auth import BearerTokenAuthMiddleware

    sent: list[dict] = []

    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        sent.append(msg)

    mw = BearerTokenAuthMiddleware(inner_app, token="secret-xyz")

    async def run_scope(path: str):
        sent.clear()
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
        }
        await mw(scope, receive, send)
        return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)

    # /console paths bypass
    assert asyncio.run(run_scope("/console")) == 200
    assert asyncio.run(run_scope("/console/")) == 200
    assert asyncio.run(run_scope("/console/dashboard")) == 200
    assert asyncio.run(run_scope("/console/assets/index-abc.js")) == 200

    # Other protected paths still require token
    assert asyncio.run(run_scope("/runs")) == 401
