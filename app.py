import uuid
import subprocess
import requests
import json
import re
import os
import ast
import asyncio
import math
import fcntl
import time
import threading
import shlex
import tempfile
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── core/: paths, config, locks, runtime state ────
from core.paths import (
    CONFIG_PATH, PROJECTS_DIR, LOG_DIR, MEMORY_DIR, REFERENCE_DIR,
    SANDBOX_DIR, SANDBOX_VENV,
    RUN_INDEX_FILE, PROMPT_INDEX, EMBED_CACHE, NEGATIVE_MEMORY, MODEL_STATS,
    SESSION_LOG, IDENTITY_FILE, PRIMER_FILE, GOALS_FILE, TARGET_IDENTITY_DIR,
    TOOL_REGISTRY_PATH,
)
from core.config import (
    CONFIG,
    OLLAMA_MAIN_URL, OLLAMA_JUDGE_URL, OLLAMA_PLANNER_URL,
    OLLAMA_MAIN, OLLAMA_JUDGE, OLLAMA_MAIN_CHAT, OLLAMA_JUDGE_CHAT,
    OLLAMA_PLANNER_CHAT, OLLAMA_PLANNER, OLLAMA_EMBED,
    HINDSIGHT_URL, HINDSIGHT_BANK, HINDSIGHT_ENABLED, HINDSIGHT_TIMEOUT,
    NOTIFY_CONFIG, NOTIFY_ENABLED, NOTIFY_SERVICE,
    NTFY_URL, NTFY_TOPIC, NTFY_PRIORITY, NTFY_URLS,
    GOTIFY_URL, GOTIFY_TOKEN, GOTIFY_PRIORITY, GOTIFY_URLS,
    NOTIFY_ON_SUCCESS, NOTIFY_ON_FAILURE,
    ORCHESTRATOR_URL, NOTIFY_STRATEGY,
    VAULT_CONFIG, VAULT_ENABLED, VAULT_LOCAL_DIR,
    VAULT_REMOTE_HOST, VAULT_REMOTE_USER, VAULT_REMOTE_KEY, VAULT_REMOTE_DIR,
    VAULT_SYNC_ENABLED, VAULT_NAS_ENABLED, VAULT_NAS_PATH,
    SSH_TARGETS, SSH_TIMEOUT, DEPLOY_BASE,
    TARGET_SCORE, MAX_ITERATIONS, MAX_TROUBLESHOOT_ATTEMPTS,
    JUDGE_FALLBACK_MODEL,
    SIMILARITY_THRESHOLD, REUSE_SCORE_THRESHOLD,
    MAX_PROMPT_INDEX_ENTRIES, MAX_EMBED_CACHE_ENTRIES,
    TIMEOUT_EMBEDDING, TIMEOUT_LLM_GENERATE, TIMEOUT_LLM_STRUCTURED,
    TIMEOUT_HINDSIGHT_RETAIN, TIMEOUT_HINDSIGHT_RECALL, TIMEOUT_HINDSIGHT_REFLECT,
    TIMEOUT_VAULT_SYNC, TIMEOUT_VAULT_NAS_SYNC,
    DREAM_AUTO_INTERVAL,
)
from core.locks import locked_read_json, locked_write_json
from core.runtime import (
    RUN_STATUS, ORCHESTRATOR_PAUSED,
    _ws_clients, _ws_lock, _MAIN_LOOP, set_main_loop,
    _ws_broadcast, log,
    _update_run_status, _init_run_status,
    _load_run_index, _persist_run_index,
)
from core.auth import BearerTokenAuthMiddleware, load_token_from_env
import core.metrics  # noqa: F401 — registers Prometheus instruments at import time

from dream import run_dream, DREAM_LOG, _load_json as dream_load_json
from gates import (
    init_gates, check_gate, record_lesson, record_runtime_failure,
    consolidate_lessons, load_gates, save_gates, add_gate, remove_gate,
    toggle_gate, get_gates_summary, get_lessons_summary, load_all_lessons,
)
from prefect_io import _healthcheck

app = FastAPI()

# Middleware ordering note (Starlette is LIFO — last-added is outermost):
# Auth is added FIRST so it ends up INNER; CORS is added SECOND so it ends
# up OUTER. Request flow: CORS → Auth → routes. This ensures Auth's 401
# responses pass back through CORS and gain Access-Control-Allow-Origin
# headers before reaching the browser.

# Bearer-token auth (Phase 1.7). No-op when ORCHESTRATOR_API_TOKEN is unset.
app.add_middleware(BearerTokenAuthMiddleware, token=load_token_from_env())

# CORS — allow the graph UI and external tools to access the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# MCP server: mount with proper lifecycle management
from mcp_server import mcp as mcp_instance
import contextlib

@contextlib.asynccontextmanager
async def _lifespan(app_instance):
    # Capture the running event loop so background threads can post coroutines
    # back via asyncio.run_coroutine_threadsafe (used by _ws_broadcast).
    set_main_loop(asyncio.get_running_loop())

    # Start log-rotation daemon (gzip >1d, delete >90d, 24-hour cadence)
    from core.log_rotation import start_rotation_daemon  # noqa: PLC0415
    start_rotation_daemon(LOG_DIR)

    try:
        if not _healthcheck():
            print("WARNING: Prefect server unreachable at startup; new runs will use daemon-thread fallback until it returns")
    except Exception as e:
        print(f"WARNING: Prefect server healthcheck failed at startup: {e}")

    # Phase 2.1 reconcile-on-startup: sweep JSON canonical state into
    # Postgres. No-op when postgres.enabled=false. Runs in a thread so
    # the async event loop doesn't block on bulk INSERTs.
    try:
        from core import config as _config  # noqa: PLC0415
        if _config.POSTGRES_ENABLED and _config.POSTGRES_RECONCILE_ON_STARTUP:
            from core.db_reconcile import reconcile_all  # noqa: PLC0415
            await asyncio.to_thread(reconcile_all)
    except Exception as e:
        print(f"WARNING: Postgres reconcile-on-startup failed: {e}")

    # Phase 2.2 hydrate-on-startup: repopulate RUN_STATUS from Redis
    # so in-flight runs survive an orchestrator restart. No-op when
    # redis.enabled=false. Runs in a thread to keep the event loop free.
    try:
        from core.runtime import hydrate_run_status_from_redis  # noqa: PLC0415
        hydrated = await asyncio.to_thread(hydrate_run_status_from_redis)
        if hydrated:
            print(f"runtime: hydrated {hydrated} RUN_STATUS entries from Redis")
    except Exception as e:
        print(f"WARNING: Redis RUN_STATUS hydrate failed at startup: {e}")

    # Phase 2.2.3 ws-pubsub: start the daemon thread that subscribes
    # to the cross-instance broadcast channel. No-op when Redis is
    # disabled. Idempotent so reload/uvicorn-reload doesn't double up.
    try:
        from core.runtime import start_ws_broadcast_subscriber  # noqa: PLC0415
        start_ws_broadcast_subscriber()
    except Exception as e:
        print(f"WARNING: Redis ws-broadcast subscriber failed to start: {e}")

    # Phase 2.3 OpenTelemetry: initialise the global TracerProvider
    # and auto-instrument FastAPI + requests. No-op when otel.enabled
    # is false. Idempotent — safe to call on reload.
    try:
        from core.otel import init_tracing  # noqa: PLC0415
        init_tracing(app)
    except Exception as e:
        print(f"WARNING: OpenTelemetry init failed at startup: {e}")

    # Phase 2.5 cloud-burst idle-stop daemon. Polls every minute
    # and stops clusters that have been idle longer than
    # ``sky.idle_timeout_minutes``. No-op when ``sky.enabled=false``;
    # the daemon will exit on its next pass if the operator flips
    # the flag back. Idempotent.
    try:
        from core.sky import start_idle_stop_daemon  # noqa: PLC0415
        start_idle_stop_daemon()
    except Exception as e:
        print(f"WARNING: SkyPilot idle-stop daemon failed to start: {e}")

    # Phase 3.3 NoteDiscovery healthcheck. No-op when
    # ``note_discovery.enabled=false``. Logs a warning if the operator
    # flipped the flag but the container is unreachable; never fatal —
    # the planner falls back to its existing memory stack on every
    # search call.
    try:
        from core.note_discovery import healthcheck, is_enabled  # noqa: PLC0415
        if is_enabled():
            if healthcheck():
                print("note_discovery: reachable")
            else:
                print("WARNING: NoteDiscovery enabled but healthcheck failed; planner will fall back to memory")
    except Exception as e:
        print(f"WARNING: NoteDiscovery healthcheck failed at startup: {e}")

    # Repo-screening spike (2026-05-12) firecrawl healthcheck. No-op
    # when ``web_ingest.enabled=false``. Logs a warning if the operator
    # flipped the flag but LXC 206 is unreachable; never fatal —
    # ``references_pkg.web.ingest_url`` fail-tolerates a down firecrawl
    # on every call.
    try:
        from references_pkg.web import healthcheck as web_health, is_enabled as web_enabled  # noqa: PLC0415
        if web_enabled():
            if web_health():
                print("firecrawl: reachable")
            else:
                print("WARNING: web_ingest enabled but firecrawl healthcheck failed; ingest_url will return http_error")
    except Exception as e:
        print(f"WARNING: firecrawl healthcheck failed at startup: {e}")

    # Phase 3.6 consumer-health daemon. Polls each registered
    # consumer's /healthz on an interval. No-op when
    # ``consumers.health_poll_seconds=0`` (the default) — a dormant
    # deploy issues no outbound probes. Idempotent.
    try:
        from core.consumer_health import start_consumer_health_daemon  # noqa: PLC0415
        start_consumer_health_daemon()
    except Exception as e:
        print(f"WARNING: consumer-health daemon failed to start: {e}")

    # Start MCP session manager (required for streamable HTTP)
    async with mcp_instance.session_manager.run():
        yield

# Attach lifespan to FastAPI app
app.router.lifespan_context = _lifespan

# Mount MCP Starlette sub-app (route handler only, lifespan managed above)
_mcp_starlette = mcp_instance.streamable_http_app()
# Remove the sub-app's own lifespan to avoid double-init
_mcp_starlette.lifespan_handler = None
app.mount("/mcp", _mcp_starlette)

from llm.ollama import (  # noqa: E402
    query_ollama_api, query_ollama, query_ollama_structured,
    resolve_chat_url, resolve_generate_url, _refresh_url_cache,
)
import llm.ollama as _llm_ollama  # for _url_cache / _url_cache_ts access in /models endpoints
from llm.repair import repair_json, safe_parse_json  # noqa: E402
from execution import (  # noqa: E402, F401
    ssh_command, deploy_file, deploy_project, persistent_deploy,
    sudo_command, validate_target,
    verify_local, verify_remote, verify_code, verify_files,
    VERIFY_CONFIG, PYTHON_STDLIB, LANG_HANDLERS,
    get_lang_handler, get_verifiable_extensions,
    detect_python_deps, detect_node_deps, detect_bash_deps,
    detect_all_dependencies, get_project_modules, sanitize_packages,
    ensure_system_dependencies, SYSTEM_DEPS, SUDO_ALLOWED, SUDO_ENABLED,
    sandbox_execute, sandbox_execute_server,
    ensure_venv, install_dependencies,
    check_port_available, find_available_port, patch_port_in_files,
    environment_inspector,
    SAFE_PKG_PATTERN,
)
from references_pkg import (  # noqa: E402, F401
    convert_pdf_to_markdown, _convert_pdf_basic, convert_file_to_markdown,
    load_reference_content, _detect_vision_model, _describe_image_with_vision,
    REFERENCE_DIR, TEXT_EXTENSIONS,
    MAX_REFERENCE_UPLOAD_BYTES, MAX_REFERENCE_CONTENT_CHARS,
)
from llm.extract import (  # noqa: E402
    extract_code, extract_files, format_files_for_prompt,
    LLM_ARTIFACTS, FILE_MARKER,
)
from tools import (  # noqa: E402, F401
    load_tool_registry, _save_tool_registry, _sanitize_tool_args,
    execute_tool, run_tools,
    TOOL_REGISTRY_PATH, _TOOL_CMD_BLOCKLIST,
)

# ── memory_pkg/: positive/negative memory, stats, layers, sessions,
#   targets, hindsight client, vault writers
from memory_pkg import (  # noqa: E402, F401
    load_prompt_index, save_prompt_index,
    load_embed_cache, save_embed_cache,
    load_negative_memory, save_negative_memory,
    load_model_stats, save_model_stats,
    generate_embedding, cosine_similarity, find_similar,
    update_memory, update_negative_memory, find_negative_matches,
    update_model_stats, get_model_recommendation, build_memory_context,
    load_identity, load_primer, rewrite_primer,
    load_goals, update_goal_status,
    load_session_log, save_session_log, record_session,
    _create_default_target_identities, load_target_identity,
    save_target_identity, auto_update_target_identity,
    hindsight_request, hindsight_ensure_bank, hindsight_retain,
    hindsight_recall, hindsight_reflect, hindsight_get_mental_models,
    hindsight_retain_file, build_hindsight_retain_content,
    format_hindsight_recall_for_planner, format_mental_models_for_planner,
    _vault_ensure_dirs, _vault_safe_name, vault_write_local,
    vault_sync_to_remote, vault_sync_file, vault_sync_to_nas,
    vault_write_run_note, vault_write_project_note, vault_write_model_note,
    vault_write_target_note, vault_write_error_note, _classify_error,
    vault_write_daily_digest, vault_write_index, vault_after_run,
)

if not PROMPT_INDEX.exists():
    PROMPT_INDEX.write_text("[]")

if not EMBED_CACHE.exists():
    EMBED_CACHE.write_text("{}")

if not NEGATIVE_MEMORY.exists():
    NEGATIVE_MEMORY.write_text("[]")

if not MODEL_STATS.exists():
    MODEL_STATS.write_text("{}")

if not SESSION_LOG.exists():
    SESSION_LOG.write_text("[]")

# create default identity.md if missing
if not IDENTITY_FILE.exists():
    IDENTITY_FILE.write_text(
        "# Identity\n\n"
        "You are an autonomous AI code orchestrator.\n"
        "Your purpose is to generate, test, judge, optimize, and deploy code.\n"
        "Prefer built-in modules. Test before deploying. Learn from failures.\n"
    )

# create default primer.md if missing
if not PRIMER_FILE.exists():
    PRIMER_FILE.write_text(
        "# Primer\n\n"
        "## Last updated\n\nNever — initial template.\n\n"
        "## Active project\n\nNo runs recorded yet.\n"
    )

# create default goals.md if missing
if not GOALS_FILE.exists():
    GOALS_FILE.write_text(
        "# Goals\n\n"
        "## Active goals\n\nNo goals defined yet.\n\n"
        "## Roadmap\n\nNo roadmap items yet.\n"
    )

# Initialize Gates safety system
init_gates()

_run_counter_since_dream = 0



# SAFE_PKG_PATTERN imported from execution above
# LLM_ARTIFACTS and FILE_MARKER imported from llm.extract above
SAFE_FILENAME = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]+$")


# Agent JSON schemas (PLAN_SCHEMA / JUDGE_SCHEMA / TOOL_DISPATCH_SCHEMA)
# now live in ``orchestration.__init__`` — loaded once via
# ``agents.loader.load_schema`` at import time. Nothing in or outside this
# file imports them from ``app``, so they're removed entirely rather than
# re-exported. See ``agents/<role>/schema.json`` for the canonical contracts.


# ------------------------------------------------
# NOTIFICATION HELPERS (multi-path with fallback)
# ------------------------------------------------
# NOTIFICATION HELPERS
# ------------------------------------------------

from notifications.send import (  # noqa: E402
    send_notification, _send_gotify, _send_ntfy,
    notify_run_complete, notify_run_started,
    send_quick_actions_notification, send_api_cheatsheet_notification,
)

# ── orchestration/: run loop, agents, context builders, OrchestrateRequest
from orchestration import (  # noqa: E402, F401
    OrchestrateRequest,
    get_loaded_models, get_available_models,
    get_orchestrator_health, get_active_runs, get_recent_completed_runs,
    get_deployed_project_count,
    gather_live_context, format_live_context_for_planner,
    build_briefing, build_full_planner_context,
    planner_agent, judge_score, generate_candidate,
    optimizer_agent, troubleshoot,
    run_orchestration,
)

# ── api/: all 82 HTTP + WebSocket routes wired via APIRouter
from api.routes import router as _api_router  # noqa: E402

app.include_router(_api_router)

# ── Phase 2.6 operator console (static SPA) ─────────────────────────────
# Mounted at /console so the legacy /ui/graph.html admin route in
# api/routes/admin.py:382 keeps working. The mount is guarded by an
# existence check on the build output so fresh checkouts that haven't
# run ``cd ui/console && npm run build`` yet won't crash on import.
#
# We use an explicit catch-all rather than ``StaticFiles(html=True)``
# alone because react-router's BrowserRouter does client-side routing —
# a request for /console/runs/abc123 must serve index.html so the SPA
# can take over, not 404. The catch-all handler probes for a real file
# first (so /console/assets/index-*.js still serves the JS bundle),
# then falls back to index.html.
from pathlib import Path as _Path  # noqa: E402

from fastapi.responses import FileResponse  # noqa: E402

_CONSOLE_DIST = _Path(__file__).parent / "ui" / "console" / "dist"
if _CONSOLE_DIST.is_dir():
    _CONSOLE_INDEX = _CONSOLE_DIST / "index.html"

    @app.get("/console", include_in_schema=False)
    @app.get("/console/", include_in_schema=False)
    async def _console_root():
        return FileResponse(_CONSOLE_INDEX)

    @app.get("/console/{path:path}", include_in_schema=False)
    async def _console_spa(path: str):
        # Disallow path-traversal — Path resolution would escape the
        # dist directory if `path` is `../foo`.
        candidate = (_CONSOLE_DIST / path).resolve()
        try:
            candidate.relative_to(_CONSOLE_DIST.resolve())
        except ValueError:
            return FileResponse(_CONSOLE_INDEX)
        if candidate.is_file():
            return FileResponse(candidate)
        # SPA fallback — client-side router will resolve the path.
        return FileResponse(_CONSOLE_INDEX)
