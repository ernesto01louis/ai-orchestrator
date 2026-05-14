"""Agents + tools + dream + gates + notifications + config + UI static routes (carved from api/routes/__init__.py).

Audit Stage 5 §D.1 split. The package-level ``router`` aggregator in
``api/routes/__init__.py`` mounts this sub-module via
``include_router``. Public URL paths and response shapes are
byte-identical to pre-split.

Imports are inherited verbatim from the pre-split preamble — some are
unused in this sub-module; a follow-up commit can trim.
"""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
)
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import llm.ollama as _llm_ollama
from agents.loader import load_agent, load_all_agents
from core.config import (
    GOTIFY_PRIORITY,
    GOTIFY_TOKEN,
    GOTIFY_URL,
    NOTIFY_ENABLED,
    NOTIFY_ON_FAILURE,
    NOTIFY_ON_SUCCESS,
    NOTIFY_SERVICE,
    NTFY_PRIORITY,
    NTFY_TOPIC,
    NTFY_URL,
)
from core.paths import (
    CONFIG_PATH,
)
from core.runtime import (
    log,
)
from dream import DREAM_LOG, run_dream
from dream import _load_json as dream_load_json
from gates import (
    add_gate,
    consolidate_lessons,
    get_gates_summary,
    get_lessons_summary,
    remove_gate,
    toggle_gate,
)
from notifications import (
    send_api_cheatsheet_notification,
    send_notification,
    send_quick_actions_notification,
)
from tools import (
    _save_tool_registry,
    load_tool_registry,
)

# Filename safety regex (was at app.py:181 originally)
# UI assets directory served by /ui routes
from ._common import SAFE_FILENAME, UI_DIR  # noqa: F401

router = APIRouter()


@router.get("/notifications/config")
def get_notification_config():
    """View current notification configuration (redacts Gotify token)."""
    return {
        "enabled": NOTIFY_ENABLED,
        "service": NOTIFY_SERVICE,
        "ntfy_url": NTFY_URL,
        "ntfy_topic": NTFY_TOPIC,
        "ntfy_priority": NTFY_PRIORITY,
        "gotify_url": GOTIFY_URL,
        "gotify_token": "***" if GOTIFY_TOKEN else "",
        "gotify_priority": GOTIFY_PRIORITY,
        "on_success": NOTIFY_ON_SUCCESS,
        "on_failure": NOTIFY_ON_FAILURE,
    }


class TestNotificationRequest(BaseModel):
    title: str | None = "Test Notification"
    message: str | None = "This is a test from the AI Orchestrator."


@router.post("/notifications/test")
def test_notification(req: TestNotificationRequest = None):
    """Send a test notification to verify the configuration."""

    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications are disabled in config")

    title = "Test Notification"
    message = "This is a test from the AI Orchestrator."

    if req:
        title = req.title or title
        message = req.message or message

    send_notification(
        title=title,
        message=message,
        priority="default",
        tags=["bell", "test_tube"]
    )

    return {"status": "sent", "service": NOTIFY_SERVICE}


@router.post("/notifications/quick-actions")
def api_quick_actions():
    """Send a remote-control notification with quick action buttons."""
    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications disabled")
    send_quick_actions_notification()
    return {"status": "sent", "service": NOTIFY_SERVICE}


@router.post("/notifications/cheatsheet")
def api_send_cheatsheet(run_id: str = None, project_name: str = None, target: str = None):
    """Send an API cheatsheet notification with curl commands."""
    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications disabled")
    send_api_cheatsheet_notification(run_id=run_id, project_name=project_name, target=target)
    return {"status": "sent"}


@router.get("/tools")
def list_tools():
    return {"tools": load_tool_registry()}


@router.post("/tools")
def create_tool(tool: dict):
    registry = load_tool_registry()
    if any(t["name"] == tool.get("name") for t in registry):
        raise HTTPException(status_code=400, detail=f"Tool '{tool.get('name')}' already exists")
    registry.append(tool)
    _save_tool_registry(registry)
    return tool


@router.put("/tools/{name}")
def update_tool(name: str, tool: dict):
    registry = load_tool_registry()
    idx = next((i for i, t in enumerate(registry) if t["name"] == name), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    registry[idx] = tool
    _save_tool_registry(registry)
    return tool


@router.delete("/tools/{name}")
def delete_tool(name: str):
    registry = load_tool_registry()
    new_registry = [t for t in registry if t["name"] != name]
    if len(new_registry) == len(registry):
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    _save_tool_registry(new_registry)
    return {"deleted": name}


# ------------------------------------------------
# AGENT CONFIG MANAGEMENT


@router.get("/agents")
def api_list_agents():
    """List all available agent roles and their configs."""
    agents = load_all_agents()
    return {"agents": {role: cfg.to_dict() for role, cfg in agents.items()}}


@router.get("/agents/{role}")
def api_get_agent(role: str):
    """Get full config for a specific agent role."""
    try:
        agent = load_agent(role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent role '{role}' not found")
    result = agent.to_dict()
    result["system_prompt"] = agent.system_prompt_template
    result["user_prompt"] = agent.user_prompt_template
    result["user_prompt_multi"] = agent.user_prompt_multi_template
    result["schema"] = agent.schema
    result["variants"] = agent.variants
    return result


@router.put("/agents/{role}/prompt/{prompt_type}")
def api_update_agent_prompt(role: str, prompt_type: str, body: dict):
    """Update a prompt file for an agent role. prompt_type: system_prompt, user_prompt, user_prompt_multi."""
    valid_types = {"system_prompt", "user_prompt", "user_prompt_multi"}
    if prompt_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"prompt_type must be one of: {valid_types}")

    try:
        agent = load_agent(role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent role '{role}' not found")

    content = body.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="Missing 'content' field")

    prompt_path = os.path.join(agent.dir, f"{prompt_type}.md")
    with open(prompt_path, "w") as f:
        f.write(content)

    # reload this agent
    load_agent(role, force_reload=True)
    return {"updated": prompt_type, "role": role}


@router.post("/agents/reload")
def api_reload_agents() -> dict[str, Any]:
    """Hot-reload every agent config from ``agents/<role>/``.

    Gated by the bearer-token middleware (Phase 1.7) like every other
    authenticated endpoint. Clears the loader cache then iterates the
    agents directory, surfacing both successful reloads and any
    per-role failures so a corrupted ``agent.yaml`` doesn't silently
    skip a role.

    Response shape::

        {
          "reloaded": ["planner", "judge", "tool_dispatch", ...],
          "failed":   [{"role": "foo", "error": "..."}],
          "count":    {"reloaded": 3, "failed": 0}
        }

    Always HTTP 200 — partial failures are domain-level, not HTTP
    errors. Returns 500 only when the agents directory itself is
    unreadable.

    This route's return shape gained ``failed`` + ``count`` keys
    (additive). The MCP-tool surface in ``mcp_server.py`` is
    unchanged, so ``MCP_CONTRACT_VERSION`` stays at ``1.0.0``.
    """
    from agents.loader import AGENTS_DIR, _cache

    _cache.clear()

    reloaded: list[str] = []
    failed: list[dict[str, str]] = []

    try:
        entries = sorted(os.listdir(AGENTS_DIR))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"cannot list agents directory: {exc}",
        ) from exc

    for entry in entries:
        agent_dir = os.path.join(AGENTS_DIR, entry)
        config_path = os.path.join(agent_dir, "agent.yaml")
        if not (os.path.isdir(agent_dir) and os.path.exists(config_path)):
            continue
        try:
            load_agent(entry, force_reload=True)
            reloaded.append(entry)
        except Exception as exc:  # noqa: BLE001 — surfaced in response
            failed.append({"role": entry, "error": str(exc)})

    import logging as _logging
    _logging.getLogger("ai_orchestrator.agents").info(
        "agents reloaded via REST: %d ok, %d failed",
        len(reloaded), len(failed),
    )

    return {
        "reloaded": reloaded,
        "failed": failed,
        "count": {"reloaded": len(reloaded), "failed": len(failed)},
    }


@router.get("/agents/{role}/variants")
def api_get_variants(role: str):
    """Get language variants for an agent role."""
    try:
        agent = load_agent(role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent role '{role}' not found")
    return {"role": role, "variants": agent.variants}


# ------------------------------------------------
# DREAM API


@router.post("/dream")
def api_run_dream():
    """Trigger a manual dream cycle (memory consolidation)."""
    available = set(_llm_ollama._url_cache.keys()) if _llm_ollama._url_cache else None
    report = run_dream(available_models=available, log_fn=log)
    return report


@router.get("/dream/log")
def api_dream_log():
    """Get dream cycle history."""
    return {"log": dream_load_json(DREAM_LOG, [])}


@router.get("/dream/health")
def api_dream_health():
    """Get current memory health without running a full dream cycle."""
    log_entries = dream_load_json(DREAM_LOG, [])
    if log_entries:
        latest = log_entries[-1]
        return {
            "health_score": latest.get("health_score", None),
            "health_rating": latest.get("health_rating", None),
            "last_dream": latest.get("timestamp", None),
            "elapsed": latest.get("elapsed", None),
        }
    return {"health_score": None, "health_rating": "unknown", "last_dream": None}


# ------------------------------------------------
# GATES API


@router.get("/gates")
def api_list_gates():
    """List all gate rules with summary stats."""
    return get_gates_summary()


@router.post("/gates")
def api_add_gate(body: dict):
    """Add a manual gate rule."""
    pattern = body.get("pattern")
    reason = body.get("reason")
    if not pattern or not reason:
        raise HTTPException(status_code=400, detail="'pattern' and 'reason' are required")
    severity = body.get("severity", "block")
    gate = add_gate(pattern, reason, source="manual", severity=severity)
    return gate


@router.delete("/gates/{gate_id}")
def api_remove_gate(gate_id: str):
    """Remove a gate rule by ID."""
    remove_gate(gate_id)
    return {"deleted": gate_id}


@router.put("/gates/{gate_id}/toggle")
def api_toggle_gate(gate_id: str, body: dict):
    """Enable or disable a gate rule."""
    enabled = body.get("enabled", True)
    toggle_gate(gate_id, enabled)
    return {"gate_id": gate_id, "enabled": enabled}


@router.get("/gates/lessons")
def api_list_lessons():
    """Get lessons summary with recent incidents."""
    return get_lessons_summary()


@router.post("/gates/consolidate")
def api_consolidate_gates(body: dict = None):
    """Manually trigger lesson consolidation. Pass dry_run=true to preview."""
    dry_run = (body or {}).get("dry_run", False)
    report = consolidate_lessons(dry_run=dry_run, log_fn=log)
    return report


@router.get("/ui", response_class=HTMLResponse)
def serve_ui():
    """Serve the 3D graph visualization."""
    graph_path = UI_DIR / "graph.html"
    if not graph_path.exists():
        raise HTTPException(status_code=404, detail="UI not found. Place graph.html in /opt/ai-orchestrator/ui/")
    return graph_path.read_text()


@router.get("/ui/{filename}")
def serve_ui_file(filename: str):
    """Serve additional UI files (CSS, JS, etc.)."""
    filepath = UI_DIR / filename
    if not filepath.exists() or ".." in filename:
        raise HTTPException(status_code=404)
    return FileResponse(filepath)


# ------------------------------------------------
# WEBSOCKET: LIVE STREAMING
# ------------------------------------------------

@router.get("/config")
def get_config():
    """Return current config.json contents."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Could not read config: {e}")


class ConfigUpdate(BaseModel):
    config: dict


@router.put("/config")
def update_config(req: ConfigUpdate):
    """
    Update config.json. Takes the full config object.
    Backs up current config before writing.
    Returns the saved config. Requires service restart to take effect.
    """

    # validate: must have required top-level keys
    required = {"ollama", "ssh_targets"}
    missing = required - set(req.config.keys())
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required keys: {missing}")

    # backup current config
    try:
        with open(CONFIG_PATH) as f:
            current = json.load(f)
    except (json.JSONDecodeError, OSError):
        current = {}

    backup_path = CONFIG_PATH + ".auto.bak"
    try:
        with open(backup_path, "w") as f:
            json.dump(current, f, indent=2)
    except OSError:
        pass

    # write new config
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(req.config, f, indent=2)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write config: {e}")

    return {"status": "saved", "config": req.config, "restart_required": True}
