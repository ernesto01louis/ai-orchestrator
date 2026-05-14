"""SkyPilot cloud-burst (Phase 2.5, dormant by default) routes (carved from api/routes/__init__.py).

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


@router.post("/runs/{run_id}/burst")
def launch_run_burst(run_id: str, body: dict):
    """Phase 2.5 cloud-burst entry point.

    Provisions a SkyPilot cluster running the named YAML spec and
    associates it with ``run_id``. Body:

        {
          "spec_name": "llm-burst",       # required
          "accelerator": "A100:1",        # optional override
          "cloud": "runpod",              # optional override
          "estimated_cost_usd": 1.25,     # required for the per-burst
                                          # cost ceiling (`sky.max_burst_cost_usd`)
          "env_overrides": {"OLLAMA_MODEL": "qwen2.5:72b"}
        }

    Returns the BurstHandle as JSON. Raises 503 when SkyPilot is
    dormant, 404 when ``spec_name`` doesn't resolve, 422 when the
    estimate exceeds the ceiling, 404 when ``run_id`` is unknown.
    """
    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    from core import sky as _sky  # noqa: PLC0415

    spec_name = body.get("spec_name")
    if not spec_name:
        raise HTTPException(status_code=422, detail="spec_name is required")

    cluster_name = body.get(
        "cluster_name", f"orch-{run_id[:8]}-{spec_name.removesuffix('.yaml')}",
    )
    req = _sky.BurstRequest(
        spec_name=str(spec_name),
        cluster_name=str(cluster_name),
        accelerator=body.get("accelerator"),
        cloud=body.get("cloud"),
        env_overrides=body.get("env_overrides"),
        detach_run=bool(body.get("detach_run", True)),
        estimated_cost_usd=float(body.get("estimated_cost_usd", 0.0) or 0.0),
    )

    try:
        handle = _sky.launch_burst(req)
    except _sky.SkyDisabledError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    _sky.register_burst(run_id, handle)
    return {
        "run_id": run_id,
        "cluster_name": handle.cluster_name,
        "spec_name": handle.spec_name,
        "started_at": handle.started_at,
        "estimated_cost_usd": handle.estimated_cost_usd,
    }


@router.get("/runs/{run_id}/bursts")
def list_run_bursts(run_id: str):
    """All cloud-burst clusters launched for ``run_id`` and still
    tracked by the orchestrator. Empty when SkyPilot is dormant or no
    bursts have been launched yet."""
    from core import sky as _sky  # noqa: PLC0415

    return {
        "run_id": run_id,
        "bursts": [
            b for b in _sky.list_registered_bursts() if b.get("run_id") == run_id
        ],
    }


@router.post("/runs/{run_id}/bursts/{cluster_name}/stop")
def stop_run_burst(run_id: str, cluster_name: str):
    """Manually stop a registered cloud-burst cluster.

    Phase 2.4 budget accrual happens here: ``cost_report_for_cluster``
    queries SkyPilot for the actual spend, and that delta gets
    accrued to the parent campaign via ``core.budget.accrue_to_campaign``.
    Returns the actual cost so callers can confirm the charge.
    """
    from core import budget as _budget  # noqa: PLC0415
    from core import sky as _sky  # noqa: PLC0415

    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    actual_cost = _sky.cost_report_for_cluster(cluster_name)
    try:
        _sky.stop_burst(cluster_name)
    except _sky.SkyDisabledError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    _sky.unregister_burst(cluster_name)
    # Accrue the actual spend to the campaign budget. No-op when
    # budget tracking is disabled, the run has no campaign, or the
    # cost is zero.
    try:
        _budget.accrue_to_campaign(run_id, float(actual_cost))
    except Exception:
        # Don't fail the stop because cost-accrual hit a snag —
        # the cluster is down, that's the load-bearing outcome.
        pass
    return {
        "run_id": run_id,
        "cluster_name": cluster_name,
        "stopped": True,
        "actual_cost_usd": float(actual_cost),
    }
