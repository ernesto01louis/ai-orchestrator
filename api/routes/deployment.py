"""SSH-target environment + deployed listing + run-deployed + delete-deployed routes (carved from api/routes/__init__.py).

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
import re
import shlex
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

import llm.ollama as _llm_ollama
from agents.loader import load_agent, load_all_agents
from core.campaign import CampaignCreate
from core.config import (
    DEPLOY_BASE,
    GOTIFY_PRIORITY,
    GOTIFY_TOKEN,
    GOTIFY_URL,
    HINDSIGHT_BANK,
    HINDSIGHT_ENABLED,
    HINDSIGHT_URL,
    NOTIFY_ENABLED,
    NOTIFY_ON_FAILURE,
    NOTIFY_ON_SUCCESS,
    NOTIFY_SERVICE,
    NTFY_PRIORITY,
    NTFY_TOPIC,
    NTFY_URL,
    OLLAMA_JUDGE_URL,
    OLLAMA_MAIN_URL,
    SSH_TARGETS,
    VAULT_ENABLED,
    VAULT_LOCAL_DIR,
    VAULT_NAS_ENABLED,
    VAULT_NAS_PATH,
    VAULT_REMOTE_DIR,
    VAULT_REMOTE_HOST,
    VAULT_SYNC_ENABLED,
)
from core.paths import (
    CAMPAIGN_TEMPLATES_DIR,
    CONFIG_PATH,
    GOALS_FILE,
    IDENTITY_FILE,
    LOG_DIR,
    NEGATIVE_MEMORY,
    PROJECTS_DIR,
    REFERENCE_DIR,
)
from core.runtime import (
    CAMPAIGN_STATUS,
    ORCHESTRATOR_PAUSED,
    RUN_STATUS,
    _campaign_status_lock,
    _init_run_status,
    _load_run_index,
    _run_status_lock,
    _update_run_status,
    _ws_clients,
    _ws_lock,
    log,
)
from dream import DREAM_LOG, run_dream
from dream import _load_json as dream_load_json
from execution import (
    environment_inspector,
    ssh_command,
    validate_target,
)
from gates import (
    add_gate,
    consolidate_lessons,
    get_gates_summary,
    get_lessons_summary,
    remove_gate,
    toggle_gate,
)
from manifest import verify_campaign_merkle, verify_run_manifest
from memory_pkg import (
    _vault_safe_name,
    auto_update_target_identity,
    find_negative_matches,
    find_similar,
    hindsight_get_mental_models,
    hindsight_recall,
    hindsight_reflect,
    hindsight_request,
    hindsight_retain,
    load_campaigns,
    load_goals,
    load_identity,
    load_model_stats,
    load_negative_memory,
    load_primer,
    load_prompt_index,
    load_session_log,
    load_target_identity,
    save_campaigns,
    save_target_identity,
    update_goal_status,
    vault_sync_to_nas,
    vault_sync_to_remote,
    vault_write_daily_digest,
    vault_write_index,
    vault_write_model_note,
    vault_write_project_note,
    vault_write_target_note,
)
from notifications import (
    send_api_cheatsheet_notification,
    send_notification,
    send_quick_actions_notification,
)
from orchestration import (
    OrchestrateRequest,
    build_briefing,
    format_live_context_for_planner,
    gather_live_context,
    get_active_runs,
    get_available_models,
    get_loaded_models,
    get_orchestrator_health,
)
from prefect_io import (
    cancel_flow_run,
    pause_flow_run,
    resume_flow_run,
    submit_campaign,
    submit_orchestration,
)
from references_pkg import (
    MAX_REFERENCE_UPLOAD_BYTES,
    convert_file_to_markdown,
)
from tools import (
    _save_tool_registry,
    load_tool_registry,
)

# Filename safety regex (was at app.py:181 originally)

# UI assets directory served by /ui routes

from ._common import SAFE_FILENAME, UI_DIR  # noqa: F401

router = APIRouter()


@router.get("/environment/{target}")
def environment(target: str):

    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = "env-scan"

    return environment_inspector(target, run_id)


@router.get("/deployed/{target}")
def list_deployed(target: str):
    """List all persistently deployed projects on a target."""

    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # resolve the deploy base
    resolve = ssh_command(target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    if not base:
        return {"target": target, "projects": []}

    # find all project.json files
    find_result = ssh_command(
        target,
        f"find {base} -maxdepth 2 -name 'project.json' -type f 2>/dev/null"
    )

    if find_result["returncode"] != 0 or not find_result["stdout"].strip():
        return {"target": target, "projects": []}

    projects = []

    for meta_path in find_result["stdout"].strip().splitlines():
        meta_path = meta_path.strip()
        if not meta_path:
            continue

        cat_result = ssh_command(target, f"cat {meta_path}")

        if cat_result["returncode"] != 0:
            continue

        try:
            meta = json.loads(cat_result["stdout"])
            projects.append(meta)
        except json.JSONDecodeError:
            continue

    # sort by deploy time, newest first
    projects.sort(key=lambda x: x.get("deployed_at", ""), reverse=True)

    return {"target": target, "projects": projects}


class RunDeployedRequest(BaseModel):

    project_name: str
    target: str
    args: str | None = None


@router.post("/run-deployed")
def run_deployed(req: RunDeployedRequest):
    """Execute an already-deployed project on the target."""

    try:
        validate_target(req.target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # resolve the deploy path
    resolve = ssh_command(req.target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    project_dir = f"{base}/{req.project_name}"

    # check that the project exists
    check = ssh_command(req.target, f"test -f {project_dir}/run.sh && echo EXISTS")

    if "EXISTS" not in check["stdout"]:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{req.project_name}' not found on {req.target}. "
                   f"Expected: {project_dir}/run.sh"
        )

    # execute it
    args_str = f" {shlex.quote(req.args)}" if req.args else ""

    try:
        execution = ssh_command(req.target, f"bash {shlex.quote(project_dir)}/run.sh{args_str}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SSH execution failed: {e}")

    return {
        "project": req.project_name,
        "target": req.target,
        "deploy_path": project_dir,
        "execution": execution
    }


class DeleteDeployedRequest(BaseModel):

    project_name: str
    target: str


@router.post("/delete-deployed")
def delete_deployed(req: DeleteDeployedRequest):
    """Remove a deployed project from a target."""

    try:
        validate_target(req.target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    resolve = ssh_command(req.target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    project_dir = f"{base}/{req.project_name}"

    # safety check: make sure we're deleting inside the deploy base
    if not project_dir.startswith(base) or ".." in req.project_name:
        raise HTTPException(status_code=400, detail="Invalid project name")

    # check it exists
    check = ssh_command(req.target, f"test -d {shlex.quote(project_dir)} && echo EXISTS")

    if "EXISTS" not in check["stdout"]:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{req.project_name}' not found on {req.target}"
        )

    try:
        ssh_command(req.target, f"rm -rf {shlex.quote(project_dir)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SSH deletion failed: {e}")

    return {
        "deleted": req.project_name,
        "target": req.target,
        "path": project_dir
    }
