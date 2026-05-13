"""Phase 2 hardening — POST /agents/reload route contract.

The pre-Phase-2 route returned ``{"reloaded": [...]}`` only. The richer
version surfaces per-role failures so a corrupted ``agent.yaml`` doesn't
silently skip a role:

    {
      "reloaded": ["planner", "judge", ...],
      "failed":   [{"role": "broken", "error": "..."}],
      "count":    {"reloaded": N, "failed": M}
    }

These tests lock that contract and prove the cache is genuinely cleared
on every call (idempotence + freshness).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def test_reload_returns_each_known_role(inprocess_client: Any) -> None:
    """Happy path: every directory under ``agents/`` that contains an
    ``agent.yaml`` ends up in ``reloaded``; ``failed`` is empty."""
    resp = inprocess_client.post("/agents/reload")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert set(body) == {"reloaded", "failed", "count"}
    assert body["failed"] == []
    assert body["count"]["failed"] == 0
    assert body["count"]["reloaded"] == len(body["reloaded"])

    # The five core roles must all reload — these have agent.yaml on disk.
    expected_roles = {"planner", "judge", "generator", "optimizer", "tool_dispatch", "troubleshooter"}
    actually_have_yaml = {
        role for role in expected_roles
        if (Path(__file__).resolve().parent.parent / "agents" / role / "agent.yaml").exists()
    }
    assert actually_have_yaml.issubset(set(body["reloaded"])), (
        f"expected roles {actually_have_yaml} all reloaded; got {body['reloaded']}"
    )


def test_reload_clears_loader_cache(inprocess_client: Any) -> None:
    """A pre-existing cached entry must NOT bypass the disk read on
    reload. We seed the loader cache with a sentinel, hit the route,
    and confirm the cache was cleared."""
    import agents.loader as loader

    loader._cache["__sentinel_role__"] = "stale-cached-value"
    inprocess_client.post("/agents/reload")
    assert "__sentinel_role__" not in loader._cache


def test_reload_surfaces_broken_role(inprocess_client: Any, tmp_path: Path) -> None:
    """A role with an unparseable ``agent.yaml`` lands in ``failed``
    with the role name + error text — the route does not return 500
    because partial failures are domain-level."""
    import agents.loader as loader

    # Stand up an isolated agents directory.
    base = tmp_path / "agents"
    base.mkdir()

    # Healthy role.
    good = base / "good_role"
    good.mkdir()
    (good / "agent.yaml").write_text("name: good_role\noutput_mode: freeform\n")

    # Broken role: agent.yaml is not valid YAML.
    bad = base / "bad_role"
    bad.mkdir()
    (bad / "agent.yaml").write_text(": this is not valid yaml [unclosed\n")

    original = loader.AGENTS_DIR
    try:
        loader.AGENTS_DIR = str(base)
        loader._cache.clear()
        resp = inprocess_client.post("/agents/reload")
    finally:
        loader.AGENTS_DIR = original

    assert resp.status_code == 200
    body = resp.json()
    assert "good_role" in body["reloaded"]
    assert any(f["role"] == "bad_role" for f in body["failed"])
    assert body["count"]["reloaded"] == 1
    assert body["count"]["failed"] == 1


def test_reload_is_idempotent(inprocess_client: Any) -> None:
    """Two consecutive calls return equivalent ``reloaded`` sets."""
    first = inprocess_client.post("/agents/reload").json()
    second = inprocess_client.post("/agents/reload").json()
    assert sorted(first["reloaded"]) == sorted(second["reloaded"])
    assert first["count"]["reloaded"] == second["count"]["reloaded"]


def test_reload_skips_dirs_without_agent_yaml(
    inprocess_client: Any, tmp_path: Path,
) -> None:
    """A directory under ``agents/`` that doesn't carry an ``agent.yaml``
    (e.g. ``__pycache__``) is not surfaced as a failed role."""
    import agents.loader as loader

    base = tmp_path / "agents"
    base.mkdir()
    (base / "__pycache__").mkdir()
    (base / "real_role").mkdir()
    (base / "real_role" / "agent.yaml").write_text("name: real_role\n")

    original = loader.AGENTS_DIR
    try:
        loader.AGENTS_DIR = str(base)
        loader._cache.clear()
        resp = inprocess_client.post("/agents/reload")
    finally:
        loader.AGENTS_DIR = original

    body = resp.json()
    assert body["reloaded"] == ["real_role"]
    assert body["failed"] == []
    # __pycache__ must NOT appear in either list.
    all_seen = set(body["reloaded"]) | {f["role"] for f in body["failed"]}
    assert "__pycache__" not in all_seen


def test_reload_returns_json_object(inprocess_client: Any) -> None:
    """Sanity check on the wire format — JSON object with the three
    documented keys, not a list or scalar."""
    resp = inprocess_client.post("/agents/reload")
    body = resp.json()
    assert isinstance(body, dict)
    assert isinstance(body["reloaded"], list)
    assert isinstance(body["failed"], list)
    assert isinstance(body["count"], dict)
    assert {"reloaded", "failed"} <= set(body["count"])


