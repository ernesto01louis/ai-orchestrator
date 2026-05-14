"""HTTP + WebSocket route handlers (package).

Originally a single ``api/routes.py`` module. Promoted to a package in
audit Stage 5 §D.1 once the file crossed the 2,500-LoC threshold the
audit itself set. The split is incremental — this ``__init__.py``
still holds the routes that haven't been carved out yet, plus the
``router`` aggregator that ``app.py`` imports.

Sub-modules expose their own ``APIRouter`` named ``router`` and are
wired in at the bottom of this file via ``router.include_router(...)``.

Tests that import function-level symbols directly from ``api.routes``
(e.g. ``from api.routes import create_campaign``) continue to work
because the package re-exports those names alongside the sub-module
imports.
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


router = APIRouter()




















# ------------------------------------------------
# API: LIST DEPLOYED PROJECTS ON A TARGET
# ------------------------------------------------



# ------------------------------------------------
# API: RUN A DEPLOYED PROJECT
# ------------------------------------------------





# ------------------------------------------------
# API: DELETE A DEPLOYED PROJECT
# ------------------------------------------------





# ------------------------------------------------
# API: MEMORY ENDPOINTS
# ------------------------------------------------











# ------------------------------------------------
# API: BRIEFING (full status overview)
# ------------------------------------------------



# ------------------------------------------------
# API: IDENTITY (Layer 1)
# ------------------------------------------------





# ------------------------------------------------
# API: PRIMER (Layer 2)
# ------------------------------------------------



# ------------------------------------------------
# API: GOALS
# ------------------------------------------------









# ------------------------------------------------
# API: SESSIONS
# ------------------------------------------------





# ------------------------------------------------
# API: LIVE CONTEXT (Layer 3)
# ------------------------------------------------





# ------------------------------------------------
# API: HINDSIGHT (Layer 4)
# ------------------------------------------------

























# ------------------------------------------------
# API: TARGET IDENTITY (per-node)
# ------------------------------------------------









# ------------------------------------------------
# API: NOTIFICATIONS
# ------------------------------------------------













# ------------------------------------------------
# API: VAULT (L5)
# ------------------------------------------------













# ------------------------------------------------
# API: DASHBOARD ENDPOINTS
# ------------------------------------------------









# ------------------------------------------------









# ------------------------------------------------





# ------------------------------------------------



















# ------------------------------------------------

UI_DIR = Path("/opt/ai-orchestrator/ui")





# ------------------------------------------------
# API: CONFIG (read / write config.json from UI)
# ------------------------------------------------







# ------------------------------------------------
# API: PROJECTS (list all deployed projects across targets)
# ------------------------------------------------



# ------------------------------------------------
# API: CAMPAIGNS (Phase 1.1)
# ------------------------------------------------



























# ------------------------------------------------
# API: EVIDENCE BUNDLES (Phase 1.2)
# ------------------------------------------------


# ---------------------------------------------------------------
# Sub-module aggregation — audit Stage 5 §D.1 split.
# ---------------------------------------------------------------
from . import (  # noqa: E402
    admin,
    burst,
    campaigns,
    content,
    deployment,
    health,
    identity,
    memory,
    runs,
    websocket,
)
from .campaigns import create_campaign  # noqa: E402, F401 — tests import this directly

router.include_router(health.router)
router.include_router(burst.router)
router.include_router(deployment.router)
router.include_router(memory.router)
router.include_router(identity.router)
router.include_router(content.router)
router.include_router(admin.router)
router.include_router(campaigns.router)
router.include_router(runs.router)
router.include_router(websocket.router)
