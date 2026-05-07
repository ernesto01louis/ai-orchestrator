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
