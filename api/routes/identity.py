"""Identity / primer / goals / sessions / briefing / live-context / targets / deployed projects routes (carved from api/routes/__init__.py).

Audit Stage 5 §D.1 split. The package-level ``router`` aggregator in
``api/routes/__init__.py`` mounts this sub-module via
``include_router``. Public URL paths and response shapes are
byte-identical to pre-split.

Imports are inherited verbatim from the pre-split preamble — some are
unused in this sub-module; a follow-up commit can trim.
"""
from __future__ import annotations

import json
import shlex

from fastapi import (
    APIRouter,
    HTTPException,
)
from pydantic import BaseModel

from core.config import (
    DEPLOY_BASE,
    SSH_TARGETS,
)
from core.paths import (
    GOALS_FILE,
    IDENTITY_FILE,
)
from execution import (
    environment_inspector,
    ssh_command,
    validate_target,
)
from memory_pkg import (
    auto_update_target_identity,
    load_goals,
    load_identity,
    load_primer,
    load_session_log,
    load_target_identity,
    save_target_identity,
    update_goal_status,
)
from orchestration import (
    build_briefing,
    format_live_context_for_planner,
    gather_live_context,
)

# Filename safety regex (was at app.py:181 originally)
# UI assets directory served by /ui routes
from ._common import SAFE_FILENAME, UI_DIR  # noqa: F401

router = APIRouter()


@router.get("/briefing")
def get_briefing():
    """
    Full status briefing combining all memory layers.
    Use this after returning from a development gap.
    """
    return build_briefing()


@router.get("/identity")
def get_identity():
    """View the current identity.md content."""
    return {"content": load_identity()}


@router.put("/identity")
def update_identity(body: dict):
    """
    Replace identity.md content.
    Expects: {"content": "...new markdown content..."}
    """
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    try:
        IDENTITY_FILE.write_text(content)
        return {"status": "updated", "length": len(content)}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write identity.md: {e}")


@router.get("/primer")
def get_primer():
    """View the current primer.md (session state)."""
    return {"content": load_primer()}


@router.get("/goals")
def get_goals():
    """View the current goals.md content."""
    return {"content": load_goals()}


@router.put("/goals")
def replace_goals(body: dict):
    """
    Replace goals.md content entirely.
    Expects: {"content": "...new markdown content..."}
    """
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    try:
        GOALS_FILE.write_text(content)
        return {"status": "updated", "length": len(content)}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write goals.md: {e}")


class GoalUpdateRequest(BaseModel):
    goal_title: str
    new_status: str | None = None
    new_phase: str | None = None
    add_decision: str | None = None


@router.post("/goals/update")
def patch_goal(req: GoalUpdateRequest):
    """Update a specific goal's status, phase, or add a decision."""
    ok = update_goal_status(
        goal_title=req.goal_title,
        new_status=req.new_status,
        new_phase=req.new_phase,
        add_decision=req.add_decision
    )
    if ok:
        return {"status": "updated", "goal": req.goal_title}
    raise HTTPException(status_code=404, detail=f"Goal '{req.goal_title}' not found")


@router.get("/sessions")
def get_sessions():
    """View recent sessions (grouped runs)."""
    sessions = load_session_log()
    # return last 20 sessions, newest first
    recent = list(reversed(sessions[-20:]))
    return {
        "total": len(sessions),
        "sessions": recent
    }


@router.get("/sessions/{session_id}")
def get_session_detail(session_id: str):
    """View details of a specific session."""
    sessions = load_session_log()
    for sess in sessions:
        if sess.get("session_id") == session_id:
            return sess
    raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")


@router.get("/live-context")
def get_live_context(target: str = "pi-1"):
    """
    Gather and return live system context.
    Includes loaded models, health, active runs, recent results.
    """
    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ctx = gather_live_context(target, "api-live-ctx")
    return ctx


@router.get("/live-context/formatted")
def get_live_context_formatted(target: str = "pi-1"):
    """
    Same as /live-context but returns the planner-formatted string.
    Useful for debugging what the planner actually sees.
    """
    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ctx = gather_live_context(target, "api-live-ctx")
    formatted = format_live_context_for_planner(ctx)
    return {"formatted": formatted}


# Health / Ollama models / Prometheus metrics routes moved to
# api/routes/health.py (audit Stage 5 §D.1). See bottom of file for
# the include_router wire-up.


@router.get("/identity/targets")
def list_target_identities():
    """List all target identity files."""
    identities = {}
    for target_name in SSH_TARGETS:
        content = load_target_identity(target_name)
        identities[target_name] = {
            "content": content,
            "length": len(content)
        }
    return {"targets": identities}


@router.get("/identity/target/{target_name}")
def get_target_identity(target_name: str):
    """View the identity.md for a specific target node."""
    if target_name not in SSH_TARGETS:
        raise HTTPException(status_code=404, detail=f"Unknown target: {target_name}")
    content = load_target_identity(target_name)
    return {"target": target_name, "content": content}


@router.put("/identity/target/{target_name}")
def update_target_identity(target_name: str, body: dict):
    """
    Replace the identity.md for a specific target node.
    Expects: {"content": "...new markdown content..."}
    """
    if target_name not in SSH_TARGETS:
        raise HTTPException(status_code=404, detail=f"Unknown target: {target_name}")

    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    ok = save_target_identity(target_name, content)
    if ok:
        return {"status": "updated", "target": target_name, "length": len(content)}
    raise HTTPException(status_code=500, detail=f"Could not write identity for {target_name}")


@router.post("/identity/target/{target_name}/profile")
def profile_target(target_name: str):
    """
    Force a re-profile of a target's hardware by running environment inspection.
    Updates the Hardware section in the target identity file.
    """
    try:
        validate_target(target_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = f"profile-{target_name}"
    env = environment_inspector(target_name, run_id)
    auto_update_target_identity(target_name, env, run_id)

    return {
        "status": "profiled",
        "target": target_name,
        "environment": env,
        "identity": load_target_identity(target_name)
    }


@router.get("/targets")
def list_targets():
    """List all configured SSH targets."""
    targets = []
    for name, cfg in SSH_TARGETS.items():
        targets.append({
            "name": name,
            "host": cfg["host"],
            "username": cfg["username"]
        })
    return {"targets": targets}


@router.get("/projects/deployed")
def list_deployed_projects():
    """List all deployed projects across all targets with metadata."""
    results = []
    for name, cfg in SSH_TARGETS.items():
        resolve = ssh_command(name, f"echo {DEPLOY_BASE}")
        base = resolve["stdout"].strip()
        if not base:
            continue

        # list project dirs
        ls_result = ssh_command(name, f"ls -1 {shlex.quote(base)} 2>/dev/null")
        if ls_result["returncode"] != 0:
            continue

        for project_name in ls_result["stdout"].strip().splitlines():
            project_name = project_name.strip()
            if not project_name:
                continue

            project_dir = f"{base}/{project_name}"

            # try to read project.json metadata
            meta_result = ssh_command(name, f"cat {shlex.quote(project_dir)}/project.json 2>/dev/null")
            meta = {}
            if meta_result["returncode"] == 0 and meta_result["stdout"].strip():
                try:
                    meta = json.loads(meta_result["stdout"])
                except (json.JSONDecodeError, ValueError):
                    pass

            results.append({
                "target": name,
                "project_name": project_name,
                "deploy_path": project_dir,
                "score": meta.get("score"),
                "language": meta.get("language"),
                "deployed_at": meta.get("deployed_at"),
                "run_id": meta.get("run_id"),
                "entrypoint": meta.get("entrypoint"),
                "prompt": meta.get("prompt", "")[:200],
            })

    return {"projects": results}
