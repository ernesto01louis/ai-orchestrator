"""Tests for the MCP contract resource and version metadata (Phase 1.7)."""

from __future__ import annotations

import json

import mcp_server


def test_contract_version_is_semver() -> None:
    parts = mcp_server.MCP_CONTRACT_VERSION.split(".")
    assert len(parts) == 3
    for part in parts:
        assert part.isdigit(), f"non-numeric semver component: {part!r}"


def test_contract_resource_payload_shape() -> None:
    func = _resource_handler("orchestrator://contract")
    raw = func()
    payload = json.loads(raw)
    assert payload["version"] == mcp_server.MCP_CONTRACT_VERSION
    assert payload["name"] == "AI Orchestrator"
    assert isinstance(payload["tools"], list)
    assert isinstance(payload["resources"], list)
    assert isinstance(payload["templates"], list)
    assert isinstance(payload["prompts"], list)


EXPECTED_TOOLS = {
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

EXPECTED_RESOURCES = {
    "orchestrator://health",
    "orchestrator://identity",
    "orchestrator://primer",
    "orchestrator://goals",
    "orchestrator://model-stats",
    "orchestrator://agents",
    "orchestrator://gates",
    "orchestrator://gates/lessons",
    "orchestrator://dream/log",
    "orchestrator://contract",
}

EXPECTED_TEMPLATES = {"orchestrator://agents/{role}"}

EXPECTED_PROMPTS = {"plan_task", "review_code", "troubleshoot_error"}


def test_contract_lists_all_known_tools() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    names = {t["name"] for t in payload["tools"]}
    assert names == EXPECTED_TOOLS, f"diff: {names ^ EXPECTED_TOOLS}"


def test_contract_lists_all_known_resources() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    uris = {r["uri"] for r in payload["resources"]}
    assert uris == EXPECTED_RESOURCES, f"diff: {uris ^ EXPECTED_RESOURCES}"


def test_contract_lists_all_known_templates() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    uris = {t["uri_template"] for t in payload["templates"]}
    assert uris == EXPECTED_TEMPLATES, f"diff: {uris ^ EXPECTED_TEMPLATES}"


def test_contract_lists_all_known_prompts() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    names = {p["name"] for p in payload["prompts"]}
    assert names == EXPECTED_PROMPTS, f"diff: {names ^ EXPECTED_PROMPTS}"


def test_every_tool_has_description() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    for tool in payload["tools"]:
        assert tool["description"], f"tool {tool['name']!r} has no description"


def _resource_handler(uri: str):
    """Pull the registered Python function for an MCP static resource."""
    for r in mcp_server.mcp._resource_manager.list_resources():
        if str(r.uri) == uri:
            return r.fn
    raise LookupError(f"no resource registered at {uri!r}")


def test_every_tool_has_category_meta() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    valid = {"orchestration", "memory", "ops", "agent_config"}
    for tool in payload["tools"]:
        meta = tool.get("meta") or {}
        assert "category" in meta, f"{tool['name']} missing meta.category"
        assert meta["category"] in valid, (
            f"{tool['name']} has unknown category {meta['category']!r}"
        )


def test_every_tool_has_requires_target_meta() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    for tool in payload["tools"]:
        meta = tool.get("meta") or {}
        assert "requires_target" in meta, (
            f"{tool['name']} missing meta.requires_target"
        )
        assert isinstance(meta["requires_target"], bool)


def test_every_tool_has_annotations() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    for tool in payload["tools"]:
        ann = tool.get("annotations") or {}
        # All 4 hints must be present (each may be None|bool — None means "unspecified").
        for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert key in ann, f"{tool['name']} missing annotations.{key}"


def test_only_orchestrate_requires_target() -> None:
    payload = json.loads(_resource_handler("orchestrator://contract")())
    require_target = {
        t["name"] for t in payload["tools"] if (t.get("meta") or {}).get("requires_target")
    }
    assert require_target == {"orchestrate"}, (
        f"unexpected requires_target tools: {require_target}"
    )
