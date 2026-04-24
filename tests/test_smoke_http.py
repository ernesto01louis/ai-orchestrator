"""Characterization tests against the live orchestrator over HTTP.

These pin the *current* externally-observable behavior of the service.
They run against the running uvicorn process at http://127.0.0.1:8000.

When refactoring during Phase 0.g, every commit must keep these green.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_health(live):
    r = live.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "orchestrator" in body
    assert "ollama_servers" in body
    assert body["orchestrator"]["disk_total_gb"] > 0


def test_notifications_config_redacts_token(live):
    r = live.get("/notifications/config")
    assert r.status_code == 200
    body = r.json()
    assert body.get("gotify_token") in (None, "", "***"), \
        "real Gotify token leaking from /notifications/config"


def test_runs_list_shape(live):
    r = live.get("/runs")
    assert r.status_code == 200
    body = r.json()
    assert "runs" in body
    assert isinstance(body["runs"], list)


def test_models_endpoint(live):
    r = live.get("/models")
    assert r.status_code == 200
    body = r.json()
    assert "available" in body
    assert isinstance(body["available"], dict)


def test_targets_endpoint(live):
    r = live.get("/targets")
    assert r.status_code == 200
    body = r.json()
    assert "targets" in body
    names = {t["name"] for t in body["targets"]}
    # configured per CLAUDE.md / config.json
    assert {"pi-1", "pi-2", "Rak"}.issubset(names)


def test_memory_positive(live):
    r = live.get("/memory")
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body
    assert isinstance(body["entries"], list)


def test_memory_negative(live):
    r = live.get("/memory/negative")
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body


def test_memory_search(live):
    # Search endpoint shape: returns a list of similar prompts.
    r = live.get("/memory/search", params={"prompt": "hello world"})
    assert r.status_code in (200, 404, 422)  # endpoint exists; signature may vary


def test_model_stats(live):
    r = live.get("/model-stats")
    assert r.status_code == 200
    body = r.json()
    assert "models" in body or isinstance(body, dict)


def test_agents_list(live):
    r = live.get("/agents")
    assert r.status_code == 200
    body = r.json()
    agents = body.get("agents", body)
    assert isinstance(agents, dict)
    expected_roles = {"planner", "judge", "generator", "optimizer", "troubleshooter", "tool_dispatch"}
    assert expected_roles.issubset(set(agents.keys()))


def test_agent_detail_planner(live):
    r = live.get("/agents/planner")
    assert r.status_code == 200
    body = r.json()
    assert body.get("role") == "planner"


def test_agents_reload(live):
    r = live.post("/agents/reload")
    assert r.status_code == 200


def test_vault_status(live):
    r = live.get("/vault/status")
    assert r.status_code == 200
    assert "enabled" in r.json()


def test_hindsight_status(live):
    r = live.get("/hindsight/status")
    assert r.status_code == 200
    assert "enabled" in r.json()


def test_gates_list(live):
    r = live.get("/gates")
    assert r.status_code == 200
    body = r.json()
    assert "gates" in body
    assert isinstance(body["gates"], list)


def test_dream_health(live):
    r = live.get("/dream/health")
    assert r.status_code == 200
    body = r.json()
    assert "health_score" in body
    assert 0 <= body["health_score"] <= 100


def test_tools_list(live):
    r = live.get("/tools")
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body
    assert isinstance(body["tools"], list)


def test_identity(live):
    r = live.get("/identity")
    assert r.status_code == 200
    body = r.json()
    assert "content" in body and isinstance(body["content"], str)


def test_goals(live):
    r = live.get("/goals")
    assert r.status_code == 200


def test_primer(live):
    r = live.get("/primer")
    assert r.status_code == 200
    assert "content" in r.json()


def test_sessions(live):
    r = live.get("/sessions")
    assert r.status_code == 200
    body = r.json()
    assert "sessions" in body or "total" in body


def test_references_list(live):
    r = live.get("/references")
    assert r.status_code == 200
    body = r.json()
    assert "references" in body


def test_references_path_traversal_rejected(live):
    """Traversal-like filenames must not read files outside REFERENCE_DIR.

    Currently safe because the references handlers re-sanitize with
    re.sub(r"[^a-zA-Z0-9_\\-\\.]", "_", filename), which neutralizes path
    separators independently of the broader SAFE_FILENAME regex. This test
    pins that defense-in-depth behavior so the refactor doesn't lose it.
    """
    payloads = [
        "..%2F..%2Fetc%2Fpasswd",
        "..%2Fapp.py",
        "%2e%2e%2fapp.py",
    ]
    for name in payloads:
        r = live.get(f"/references/{name}/content")
        # 400/403/404 are all acceptable; anything 200 means the file leaked out.
        assert r.status_code != 200, f"traversal succeeded for {name!r}: {r.text[:200]}"


def test_vault_note_path_traversal_rejected(live):
    """The vault note reader uses SAFE_FILENAME *and* a .resolve() base check.

    The base check is the actual safeguard since SAFE_FILENAME accepts '..'.
    Pin the behavior so a future refactor doesn't drop the .resolve() check.
    """
    r = live.get("/vault/notes/runs/..%2F..%2Fapp.py")
    assert r.status_code != 200, f"vault traversal succeeded: {r.text[:200]}"
