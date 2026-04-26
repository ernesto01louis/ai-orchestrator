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

from agents.loader import load_agent, load_all_agents, reload_all as reload_agents, list_roles as list_agent_roles
from dream import run_dream, DREAM_LOG, _load_json as dream_load_json
from gates import (
    init_gates, check_gate, record_lesson, record_runtime_failure,
    consolidate_lessons, load_gates, save_gates, add_gate, remove_gate,
    toggle_gate, get_gates_summary, get_lessons_summary, load_all_lessons,
)

app = FastAPI()

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


# ------------------------------------------------
# JSON SCHEMAS FOR STRUCTURED OUTPUT
# ------------------------------------------------

# Schemas loaded from agents/ configs — fall back to inline defaults if loader fails
def _load_agent_schema(role, fallback):
    try:
        agent = load_agent(role)
        if agent.schema:
            return agent.schema
    except Exception:
        pass
    return fallback

PLAN_SCHEMA = _load_agent_schema("planner", {
    "type": "object",
    "properties": {
        "language": {"type": "string"}, "entrypoint": {"type": "string"},
        "project_type": {"type": "string"}, "execution_mode": {"type": "string"},
        "port": {"type": "integer"},
        "files": {"type": "object", "additionalProperties": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["language", "entrypoint", "project_type", "execution_mode", "files", "dependencies", "steps"]
})

JUDGE_SCHEMA = _load_agent_schema("judge", {
    "type": "object",
    "properties": {
        "correctness": {"type": "number"}, "robustness": {"type": "number"},
        "security": {"type": "number"}, "performance": {"type": "number"},
        "structure": {"type": "number"}, "overall": {"type": "number"},
        "improvements": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["correctness", "robustness", "security", "performance", "overall", "improvements"]
})

TOOL_DISPATCH_SCHEMA = _load_agent_schema("tool_dispatch", {
    "type": "object",
    "properties": {
        "tools": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "args": {"type": "object", "additionalProperties": {"type": "string"}}},
            "required": ["name"]
        }}
    },
    "required": ["tools"]
})



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


@app.post("/orchestrate")
def orchestrate(req: OrchestrateRequest):

    if ORCHESTRATOR_PAUSED:
        raise HTTPException(status_code=503, detail="Orchestrator is paused")

    try:
        validate_target(req.deploy_target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = str(uuid.uuid4())

    _init_run_status(run_id, project=req.project_name, target=req.deploy_target)

    thread = threading.Thread(
        target=run_orchestration,
        args=(req, run_id),
        daemon=True
    )
    thread.start()

    return {
        "run_id": run_id,
        "status": "started",
        "poll": f"/status/{run_id}"
    }


@app.get("/status/{run_id}")
def status(run_id: str):

    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    info = RUN_STATUS[run_id]

    response = {
        "run_id": run_id,
        "phase": info["phase"],
        "score": info["score"],
        "completed": info["completed"],
    }

    if info.get("project"):
        response["project"] = info["project"]

    if info.get("target"):
        response["target"] = info["target"]

    if info["completed"]:

        if info["error"]:
            response["error"] = info["error"]

        if info["result"]:
            response["result"] = info["result"]

    return response


@app.get("/result/{run_id}")
def result(run_id: str):

    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    info = RUN_STATUS[run_id]

    if not info["completed"]:
        return {
            "run_id": run_id,
            "status": "running",
            "phase": info["phase"],
            "score": info["score"]
        }

    if info["error"]:
        raise HTTPException(status_code=500, detail=info["error"])

    return info["result"]


@app.get("/runs")
def list_runs():
    """List runs — combines in-memory active runs with persistent history from log files and run index."""

    # 1. Load persistent run index (fast, no SSH)
    run_index = _load_run_index()

    seen = set()
    runs = []

    # 2. In-memory active/recent runs (highest priority)
    for rid, info in RUN_STATUS.items():
        seen.add(rid)
        indexed = run_index.get(rid, {})
        runs.append({
            "run_id": rid,
            "phase": info["phase"],
            "score": info["score"] or indexed.get("score", 0),
            "completed": info["completed"],
            "project": info.get("project") or indexed.get("project", ""),
            "target": info.get("target") or indexed.get("target", ""),
            "has_error": info.get("error") is not None,
            "timestamp": indexed.get("timestamp"),
        })

    # 3. Runs from persistent index (survived restarts)
    for rid, indexed in run_index.items():
        if rid in seen:
            continue
        seen.add(rid)
        runs.append({
            "run_id": rid,
            "phase": indexed.get("phase", "completed"),
            "score": indexed.get("score", 0),
            "completed": True,
            "project": indexed.get("project", ""),
            "target": indexed.get("target", ""),
            "has_error": indexed.get("has_error", False),
            "timestamp": indexed.get("timestamp"),
        })

    # 4. Historical runs from log files (catch runs not yet in index)
    log_dir = Path(LOG_DIR)
    if log_dir.exists():
        log_files = sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        for lf in log_files[:100]:
            rid = lf.stem
            if rid in seen or rid == "graph-data":
                continue
            seen.add(rid)

            # Extract info from log content
            phase = "completed"
            has_error = False
            log_project = ""
            log_target = ""
            try:
                content = lf.read_text(errors="replace")
                all_lines = content.strip().splitlines()
                if all_lines:
                    last = all_lines[-1].lower()
                    if "error" in last or "crash" in last or "failed" in last:
                        phase = "failed"
                        has_error = True
                    for line in all_lines:
                        if "persistent deploy" in line and "->" in line:
                            m = re.search(r"->\s*(\S+):.*/([^/\s]+)\s*$", line)
                            if m:
                                log_target = m.group(1)
                                log_project = m.group(2)
                            break
            except OSError:
                pass

            ts = datetime.utcfromtimestamp(lf.stat().st_mtime).isoformat() if lf.exists() else None

            runs.append({
                "run_id": rid,
                "phase": phase,
                "score": 0,
                "completed": True,
                "project": log_project,
                "target": log_target,
                "has_error": has_error,
                "timestamp": ts,
            })

    # Sort: active (not completed) first, then by timestamp descending
    runs.sort(key=lambda r: (
        r.get("completed", True),
        r.get("timestamp") or "0000",
    ), reverse=True)

    return {"runs": runs}


@app.get("/files/{run_id}")
def get_files(run_id: str):

    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    info = RUN_STATUS[run_id]

    if not info["completed"]:
        raise HTTPException(status_code=409, detail="Run not yet completed")

    project = info.get("project", "")

    files_path = Path(PROJECTS_DIR) / project / "runs" / run_id / "files.json"

    if not files_path.exists():
        raise HTTPException(status_code=404, detail="No files found for this run")

    try:
        files = json.loads(files_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Could not read files: {e}")

    return {
        "run_id": run_id,
        "project": project,
        "files": files
    }


@app.get("/environment/{target}")
def environment(target: str):

    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = "env-scan"

    return environment_inspector(target, run_id)


# ------------------------------------------------
# API: LIST DEPLOYED PROJECTS ON A TARGET
# ------------------------------------------------

@app.get("/deployed/{target}")
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


# ------------------------------------------------
# API: RUN A DEPLOYED PROJECT
# ------------------------------------------------

class RunDeployedRequest(BaseModel):

    project_name: str
    target: str
    args: str | None = None


@app.post("/run-deployed")
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


# ------------------------------------------------
# API: DELETE A DEPLOYED PROJECT
# ------------------------------------------------

class DeleteDeployedRequest(BaseModel):

    project_name: str
    target: str


@app.post("/delete-deployed")
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


# ------------------------------------------------
# API: MEMORY ENDPOINTS
# ------------------------------------------------

@app.get("/memory")
def get_memory():
    """View positive memory (past runs with similarity data)."""

    index = load_prompt_index()

    # return without embeddings (they're huge)
    entries = []
    for entry in index:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        entries.append(e)

    entries.reverse()  # newest first

    return {
        "total": len(entries),
        "entries": entries[:50]  # last 50
    }


@app.get("/memory/negative")
def get_negative_memory():
    """View negative memory (past failures)."""

    entries = load_negative_memory()

    # return without embeddings
    clean = []
    for entry in entries:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        clean.append(e)

    clean.reverse()  # newest first

    return {
        "total": len(clean),
        "entries": clean[:50]
    }


@app.get("/memory/search")
def search_memory(q: str):
    """Search memory by semantic similarity to a query string."""

    positive = find_similar(q)
    negative = find_negative_matches(q)

    pos_results = []
    for sim_score, entry in positive:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        e["similarity"] = round(sim_score, 4)
        pos_results.append(e)

    neg_results = []
    for sim_score, entry in negative:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        e["similarity"] = round(sim_score, 4)
        neg_results.append(e)

    return {
        "query": q,
        "positive_matches": pos_results,
        "negative_matches": neg_results
    }


@app.get("/model-stats")
def get_model_stats_endpoint():
    """View model performance statistics."""

    stats = load_model_stats()

    # compute derived metrics
    summary = {}
    for model, s in stats.items():
        total = s["total_runs"]
        avg_score = s["total_score"] / total if total > 0 else 0
        win_rate = (s["wins"] / total * 100) if total > 0 else 0
        fail_rate = (s["failures"] / total * 100) if total > 0 else 0

        summary[model] = {
            "total_runs": total,
            "avg_score": round(avg_score, 2),
            "win_rate": round(win_rate, 1),
            "fail_rate": round(fail_rate, 1),
            "wins": s["wins"],
            "failures": s["failures"],
            "by_language": s["by_language"],
            "by_role": s["by_role"],
            "by_project_type": s["by_project_type"],
            "recent_trend": [r["score"] for r in s.get("recent_scores", [])]
        }

    return {"models": summary}


@app.get("/model-stats/{model_name}")
def get_single_model_stats(model_name: str):
    """View detailed stats for a specific model."""

    stats = load_model_stats()

    # url-decode the model name (e.g. qwen2.5-coder%3A32b -> qwen2.5-coder:32b)
    import urllib.parse
    model_name = urllib.parse.unquote(model_name)

    if model_name not in stats:
        raise HTTPException(status_code=404, detail=f"No stats for model '{model_name}'")

    s = stats[model_name]
    total = s["total_runs"]

    return {
        "model": model_name,
        "total_runs": total,
        "avg_score": round(s["total_score"] / total, 2) if total > 0 else 0,
        "win_rate": round((s["wins"] / total * 100), 1) if total > 0 else 0,
        "fail_rate": round((s["failures"] / total * 100), 1) if total > 0 else 0,
        "by_language": s["by_language"],
        "by_role": s["by_role"],
        "by_project_type": s["by_project_type"],
        "recent_scores": s.get("recent_scores", [])
    }


# ------------------------------------------------
# API: BRIEFING (full status overview)
# ------------------------------------------------

@app.get("/briefing")
def get_briefing():
    """
    Full status briefing combining all memory layers.
    Use this after returning from a development gap.
    """
    return build_briefing()


# ------------------------------------------------
# API: IDENTITY (Layer 1)
# ------------------------------------------------

@app.get("/identity")
def get_identity():
    """View the current identity.md content."""
    return {"content": load_identity()}


@app.put("/identity")
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


# ------------------------------------------------
# API: PRIMER (Layer 2)
# ------------------------------------------------

@app.get("/primer")
def get_primer():
    """View the current primer.md (session state)."""
    return {"content": load_primer()}


# ------------------------------------------------
# API: GOALS
# ------------------------------------------------

@app.get("/goals")
def get_goals():
    """View the current goals.md content."""
    return {"content": load_goals()}


@app.put("/goals")
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


@app.post("/goals/update")
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


# ------------------------------------------------
# API: SESSIONS
# ------------------------------------------------

@app.get("/sessions")
def get_sessions():
    """View recent sessions (grouped runs)."""
    sessions = load_session_log()
    # return last 20 sessions, newest first
    recent = list(reversed(sessions[-20:]))
    return {
        "total": len(sessions),
        "sessions": recent
    }


@app.get("/sessions/{session_id}")
def get_session_detail(session_id: str):
    """View details of a specific session."""
    sessions = load_session_log()
    for sess in sessions:
        if sess.get("session_id") == session_id:
            return sess
    raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")


# ------------------------------------------------
# API: LIVE CONTEXT (Layer 3)
# ------------------------------------------------

@app.get("/live-context")
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


@app.get("/live-context/formatted")
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


# ------------------------------------------------
# API: OLLAMA MODELS
# ------------------------------------------------

@app.get("/models")
def get_models():
    """List all available and currently loaded models across both Ollama servers."""
    return {
        "loaded": get_loaded_models(),
        "available": get_available_models()
    }


@app.get("/models/loaded")
def get_models_loaded():
    """Quick check: which models are currently hot in memory."""
    return {"loaded": get_loaded_models()}


# ------------------------------------------------
# API: SYSTEM HEALTH
# ------------------------------------------------

@app.get("/health")
def get_health():
    """Orchestrator system health check."""

    health = get_orchestrator_health()
    active = get_active_runs()

    # check Ollama server connectivity
    ollama_status = {}
    for name, url in [("main", OLLAMA_MAIN_URL), ("judge", OLLAMA_JUDGE_URL)]:
        try:
            r = requests.get(f"{url}/api/tags", timeout=5)
            ollama_status[name] = {
                "url": url,
                "status": "online" if r.status_code == 200 else f"error ({r.status_code})",
                "model_count": len(r.json().get("models", [])) if r.status_code == 200 else 0
            }
        except requests.exceptions.RequestException as e:
            ollama_status[name] = {
                "url": url,
                "status": f"offline ({type(e).__name__})",
                "model_count": 0
            }

    # check Hindsight connectivity
    hindsight_health = {"enabled": HINDSIGHT_ENABLED}
    if HINDSIGHT_ENABLED:
        try:
            r = requests.get(f"{HINDSIGHT_URL}/v1/default/banks", timeout=5)
            hindsight_health["status"] = "online" if r.status_code == 200 else f"error ({r.status_code})"
            hindsight_health["url"] = HINDSIGHT_URL
        except requests.exceptions.RequestException as e:
            hindsight_health["status"] = f"offline ({type(e).__name__})"
            hindsight_health["url"] = HINDSIGHT_URL

    return {
        "orchestrator": health,
        "ollama_servers": ollama_status,
        "hindsight": hindsight_health,
        "active_runs": len(active),
        "uptime_indicator": "ok"
    }


# ------------------------------------------------
# API: HINDSIGHT (Layer 4)
# ------------------------------------------------

@app.get("/hindsight/status")
def hindsight_status():
    """Check if Hindsight is reachable and get bank info."""

    if not HINDSIGHT_ENABLED:
        return {"enabled": False, "status": "disabled in config"}

    try:
        r = requests.get(f"{HINDSIGHT_URL}/v1/default/banks", timeout=10)
        r.raise_for_status()
        banks = r.json()

        return {
            "enabled": True,
            "url": HINDSIGHT_URL,
            "bank_id": HINDSIGHT_BANK,
            "status": "online",
            "banks": banks
        }
    except requests.exceptions.RequestException as e:
        return {
            "enabled": True,
            "url": HINDSIGHT_URL,
            "bank_id": HINDSIGHT_BANK,
            "status": f"offline ({type(e).__name__})"
        }


class HindsightRecallRequest(BaseModel):
    query: str
    max_tokens: int = 2000


@app.post("/hindsight/recall")
def api_hindsight_recall(req: HindsightRecallRequest):
    """Recall memories from Hindsight for a given query."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_recall(req.query, "api-recall", max_tokens=req.max_tokens)

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight recall failed")

    return result


class HindsightRetainRequest(BaseModel):
    content: str


@app.post("/hindsight/retain")
def api_hindsight_retain(req: HindsightRetainRequest):
    """Manually store a memory in Hindsight."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_retain(req.content, "api-retain")

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight retain failed")

    return result


class HindsightReflectRequest(BaseModel):
    query: str


@app.post("/hindsight/reflect")
def api_hindsight_reflect(req: HindsightReflectRequest):
    """
    Ask Hindsight to reflect on accumulated memories.
    This synthesizes observations and opinions from past experiences.
    Can take 1-5 minutes depending on memory volume and model speed.
    """

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_reflect(req.query, "api-reflect")

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight reflect failed")

    return result


# ── Mental Models API ─────────────────────────────

@app.get("/hindsight/mental-models")
def api_mental_models():
    """List all Hindsight mental models with their content."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    models = hindsight_get_mental_models("api")
    return {"models": models}


@app.post("/hindsight/mental-models/{model_id}/refresh")
def api_refresh_mental_model(model_id: str):
    """Trigger a refresh of a specific mental model."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_request(
        "POST",
        f"/v1/default/banks/{HINDSIGHT_BANK}/mental-models/{model_id}/refresh",
        timeout=30
    )

    if result is None:
        raise HTTPException(status_code=502, detail="Mental model refresh failed")

    return result



@app.post("/references/upload")
async def upload_reference(file: UploadFile = File(...)):
    """
    Upload a file as a reference document.
    PDFs are auto-converted to markdown. Text files are stored as-is.
    Everything gets ingested into Hindsight for RAG.
    """

    filename = file.filename or f"ref_{uuid.uuid4().hex[:8]}"
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    local_path = REFERENCE_DIR / safe_name

    # save original file with size limit
    content = await file.read()
    if len(content) > MAX_REFERENCE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large ({len(content)//1024//1024}MB). Max {MAX_REFERENCE_UPLOAD_BYTES//1024//1024}MB.")
    with open(local_path, "wb") as f:
        f.write(content)

    # convert to markdown
    md_text, meta = convert_file_to_markdown(str(local_path), "api-upload")

    # save markdown version alongside original (for PDF → .md)
    md_filename = f"{Path(safe_name).stem}.md"
    md_path = REFERENCE_DIR / md_filename
    if local_path.suffix.lower() != ".md":
        md_path.write_text(md_text)

    # ingest markdown into Hindsight
    hindsight_result = None
    if HINDSIGHT_ENABLED:
        hindsight_result = hindsight_retain(
            f"Reference document '{filename}' uploaded.\n\n{md_text[:3000]}",
            "api-upload"
        )

    return {
        "filename": safe_name,
        "markdown_filename": md_filename,
        "path": str(local_path),
        "size": len(content),
        "markdown_size": len(md_text),
        "conversion": meta,
        "hindsight_ingested": hindsight_result is not None,
    }


@app.get("/references")
def list_references():
    """List all uploaded reference documents with their markdown status."""

    refs = []
    seen_stems = set()

    for f in sorted(REFERENCE_DIR.iterdir()):
        try:
            if not f.is_file() or f.name.endswith("_images"):
                continue
            stem = f.stem
            # skip markdown duplicates of PDFs (show the original instead)
            if f.suffix.lower() == ".md" and stem in seen_stems:
                continue

            md_path = REFERENCE_DIR / f"{stem}.md"
            has_markdown = md_path.exists() and f.suffix.lower() != ".md"
            st = f.stat()

            refs.append({
                "filename": f.name,
                "path": str(f),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "type": f.suffix.lstrip(".").lower() or "unknown",
                "has_markdown": has_markdown,
                "markdown_filename": f"{stem}.md" if has_markdown else None,
            })
            seen_stems.add(stem)
        except (FileNotFoundError, OSError):
            continue

    return {"references": refs}


@app.get("/references/{filename}/content")
def get_reference_content(filename: str):
    """Get the markdown content of a reference (converted if PDF)."""

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    stem = Path(safe_name).stem

    # try markdown version first
    md_path = REFERENCE_DIR / f"{stem}.md"
    if md_path.exists():
        return {"filename": safe_name, "content": md_path.read_text(errors="replace"), "format": "markdown"}

    # try original
    orig_path = REFERENCE_DIR / safe_name
    if not orig_path.exists():
        raise HTTPException(status_code=404, detail="Reference not found")

    try:
        content = orig_path.read_text(errors="replace")
        return {"filename": safe_name, "content": content, "format": "text"}
    except Exception:
        raise HTTPException(status_code=415, detail="Cannot read file as text")


@app.delete("/references/{filename}")
def delete_reference(filename: str):
    """Delete a reference document and its converted markdown."""

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    path = REFERENCE_DIR / safe_name
    stem = Path(safe_name).stem

    if not path.exists():
        raise HTTPException(status_code=404, detail="Reference not found")

    path.unlink(missing_ok=True)

    # also remove markdown version if it exists
    md_path = REFERENCE_DIR / f"{stem}.md"
    if md_path != path:
        md_path.unlink(missing_ok=True)

    # remove extracted images dir if it exists
    img_dir = REFERENCE_DIR / f"{stem}_images"
    if img_dir.exists() and img_dir.is_dir():
        import shutil
        shutil.rmtree(img_dir, ignore_errors=True)

    return {"deleted": safe_name}


# ── Graph Data API (for enriched UI nodes) ────────

@app.get("/graph-data")
def get_graph_data():
    """
    Combined endpoint for the UI node cluster.
    Returns nodes (projects, models, targets, errors) with enriched metadata
    from vault notes, memory, and mental models.
    """

    # load all memory sources
    index = load_prompt_index()
    neg_memory = json.loads(NEGATIVE_MEMORY.read_text()) if NEGATIVE_MEMORY.exists() else []
    model_stats = load_model_stats()

    # build project summaries
    projects = {}
    for entry in index:
        name = entry.get("project", "")
        if not name:
            continue
        if name not in projects:
            projects[name] = {
                "type": "project", "name": name,
                "runs": 0, "successes": 0, "best_score": 0,
                "languages": set(), "models": set(), "targets": set(),
                "last_prompt": "", "last_run": "",
            }
        p = projects[name]
        p["runs"] += 1
        if entry.get("success"):
            p["successes"] += 1
        p["best_score"] = max(p["best_score"], entry.get("score", 0))
        if entry.get("language"):
            p["languages"].add(entry["language"])
        if entry.get("winning_model"):
            p["models"].add(entry["winning_model"])
        p["last_prompt"] = entry.get("prompt", p["last_prompt"])
        p["last_run"] = entry.get("run_id", p["last_run"])

    # serialize sets
    for p in projects.values():
        p["languages"] = list(p["languages"])
        p["models"] = list(p["models"])
        p["targets"] = list(p["targets"])
        p["success_rate"] = round(p["successes"] / max(p["runs"], 1) * 100)

    # build error summaries from negative memory
    errors = []
    for entry in neg_memory[-20:]:
        errors.append({
            "type": "error",
            "project": entry.get("project", ""),
            "stage": entry.get("failure_stage", "unknown"),
            "summary": (entry.get("error_summary", ""))[:100],
            "models": entry.get("models_tried", []),
            "language": entry.get("language", ""),
        })

    # build model summaries
    models = {}
    for name, stats in model_stats.items():
        models[name] = {
            "type": "model", "name": name,
            **stats,
        }

    # build target summaries
    targets = {}
    for name in SSH_TARGETS:
        targets[name] = {
            "type": "target", "name": name,
            "host": SSH_TARGETS[name].get("host", ""),
        }
        # load target identity if available
        identity = load_target_identity(name)
        if identity:
            targets[name]["identity"] = identity[:500]

    # mental models (if available)
    mental_models = []
    if HINDSIGHT_ENABLED:
        mental_models = hindsight_get_mental_models("graph-data")

    return {
        "projects": list(projects.values()),
        "models": list(models.values()),
        "targets": list(targets.values()),
        "errors": errors,
        "mental_models": [
            {"id": m["id"], "name": m["name"], "content": m.get("content", "")[:1000],
             "tags": m.get("tags", []), "last_refreshed": m.get("last_refreshed_at", "")}
            for m in mental_models
        ],
    }


# ------------------------------------------------
# API: TARGET IDENTITY (per-node)
# ------------------------------------------------

@app.get("/identity/targets")
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


@app.get("/identity/target/{target_name}")
def get_target_identity(target_name: str):
    """View the identity.md for a specific target node."""
    if target_name not in SSH_TARGETS:
        raise HTTPException(status_code=404, detail=f"Unknown target: {target_name}")
    content = load_target_identity(target_name)
    return {"target": target_name, "content": content}


@app.put("/identity/target/{target_name}")
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


@app.post("/identity/target/{target_name}/profile")
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


# ------------------------------------------------
# API: NOTIFICATIONS
# ------------------------------------------------

@app.get("/notifications/config")
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


@app.post("/notifications/test")
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


@app.post("/notifications/quick-actions")
def api_quick_actions():
    """Send a remote-control notification with quick action buttons."""
    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications disabled")
    send_quick_actions_notification()
    return {"status": "sent", "service": NOTIFY_SERVICE}


@app.post("/notifications/cheatsheet")
def api_send_cheatsheet(run_id: str = None, project_name: str = None, target: str = None):
    """Send an API cheatsheet notification with curl commands."""
    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications disabled")
    send_api_cheatsheet_notification(run_id=run_id, project_name=project_name, target=target)
    return {"status": "sent"}




# ------------------------------------------------
# API: VAULT (L5)
# ------------------------------------------------

@app.get("/vault/status")
def vault_status():
    """Check vault configuration and note counts."""
    counts = {}
    for subdir in ["runs", "projects", "models", "targets", "errors", "daily"]:
        dir_path = Path(VAULT_LOCAL_DIR) / subdir
        if dir_path.exists():
            counts[subdir] = len(list(dir_path.glob("*.md")))
        else:
            counts[subdir] = 0

    return {
        "enabled": VAULT_ENABLED,
        "local_dir": VAULT_LOCAL_DIR,
        "remote_host": VAULT_REMOTE_HOST or None,
        "remote_dir": VAULT_REMOTE_DIR if VAULT_REMOTE_HOST else None,
        "sync_enabled": VAULT_SYNC_ENABLED,
        "nas_enabled": VAULT_NAS_ENABLED,
        "nas_path": VAULT_NAS_PATH if VAULT_NAS_ENABLED else None,
        "note_counts": counts,
        "total_notes": sum(counts.values())
    }


@app.post("/vault/sync")
def vault_force_sync():
    """Force a full vault sync to the remote NoteDiscovery host."""
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")
    if not VAULT_REMOTE_HOST:
        raise HTTPException(status_code=400, detail="No remote host configured")

    ok_remote = vault_sync_to_remote("api-vault-sync")
    ok_nas = False
    if VAULT_NAS_ENABLED:
        ok_nas = vault_sync_to_nas("api-vault-sync")
    return {
        "status": "synced",
        "notediscovery": "synced" if ok_remote else "failed",
        "nas": "synced" if ok_nas else ("failed" if VAULT_NAS_ENABLED else "disabled")
    }


@app.post("/vault/sync-nas")
def vault_force_sync_nas():
    """Force sync vault to NAS mount point."""
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")
    if not VAULT_NAS_ENABLED:
        raise HTTPException(status_code=503, detail="NAS sync is disabled")

    ok = vault_sync_to_nas("api-vault-nas")
    return {"status": "synced" if ok else "failed", "path": VAULT_NAS_PATH}


@app.post("/vault/rebuild")
def vault_rebuild():
    """
    Rebuild all vault notes from current memory state.
    Useful after importing data or fixing issues.
    Regenerates project notes, model notes, target notes, daily digest, and index.
    """
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")

    run_id = "vault-rebuild"
    rebuilt = []

    # rebuild project notes
    index = load_prompt_index()
    projects = list(set(e.get("project", "") for e in index if e.get("project")))
    for project in projects:
        vault_write_project_note(project, run_id)
        rebuilt.append(f"projects/{_vault_safe_name(project)}")

    # rebuild model notes
    stats = load_model_stats()
    for model_name in stats:
        vault_write_model_note(model_name, run_id)
        rebuilt.append(f"models/{_vault_safe_name(model_name)}")

    # rebuild target notes
    for target_name in SSH_TARGETS:
        vault_write_target_note(target_name, run_id)
        rebuilt.append(f"targets/{_vault_safe_name(target_name)}")

    # daily digest
    vault_write_daily_digest(run_id)
    rebuilt.append("daily digest")

    # index
    vault_write_index(run_id)
    rebuilt.append("index")

    # sync all
    if VAULT_SYNC_ENABLED and VAULT_REMOTE_HOST:
        vault_sync_to_remote(run_id)

    return {"status": "rebuilt", "notes": rebuilt}


@app.get("/vault/note/{subdir}/{filename}")
def vault_get_note(subdir: str, filename: str):
    """Read a specific vault note."""
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")

    if subdir not in ("runs", "projects", "models", "targets", "errors", "daily"):
        raise HTTPException(status_code=400, detail="Invalid vault subdirectory")

    if not SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # prevent path traversal via symlinks or ..
    filepath = (Path(VAULT_LOCAL_DIR) / subdir / filename).resolve()
    vault_base = Path(VAULT_LOCAL_DIR).resolve()
    if not str(filepath).startswith(str(vault_base)):
        raise HTTPException(status_code=400, detail="Invalid path")

    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Note not found")

    try:
        content = filepath.read_text()
        return {"subdir": subdir, "filename": filename, "content": content}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not read note: {e}")


@app.post("/hindsight/reflect/auto")
def api_hindsight_auto_reflect():
    """
    Trigger automatic reflection on key orchestrator topics.
    Asks Hindsight to synthesize insights about model performance,
    common failure patterns, and language/task-type trends.
    """

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    queries = [
        "Which models perform best for which programming languages and task types?",
        "What are the most common failure patterns and how can they be avoided?",
        "What deployment and execution patterns have been most reliable?",
    ]

    results = []
    for q in queries:
        result = hindsight_reflect(q, "auto-reflect")
        results.append({
            "query": q,
            "result": result
        })

    return {"reflections": results}


# ------------------------------------------------
# API: DASHBOARD ENDPOINTS
# ------------------------------------------------

@app.get("/targets")
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


@app.get("/tools")
def list_tools():
    return {"tools": load_tool_registry()}


@app.post("/tools")
def create_tool(tool: dict):
    registry = load_tool_registry()
    if any(t["name"] == tool.get("name") for t in registry):
        raise HTTPException(status_code=400, detail=f"Tool '{tool.get('name')}' already exists")
    registry.append(tool)
    _save_tool_registry(registry)
    return tool


@app.put("/tools/{name}")
def update_tool(name: str, tool: dict):
    registry = load_tool_registry()
    idx = next((i for i, t in enumerate(registry) if t["name"] == name), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    registry[idx] = tool
    _save_tool_registry(registry)
    return tool


@app.delete("/tools/{name}")
def delete_tool(name: str):
    registry = load_tool_registry()
    new_registry = [t for t in registry if t["name"] != name]
    if len(new_registry) == len(registry):
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    _save_tool_registry(new_registry)
    return {"deleted": name}


# ------------------------------------------------
# AGENT CONFIG MANAGEMENT
# ------------------------------------------------

@app.get("/agents")
def api_list_agents():
    """List all available agent roles and their configs."""
    agents = load_all_agents()
    return {"agents": {role: cfg.to_dict() for role, cfg in agents.items()}}


@app.get("/agents/{role}")
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


@app.put("/agents/{role}/prompt/{prompt_type}")
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


@app.post("/agents/reload")
def api_reload_agents():
    """Force-reload all agent configs from disk."""
    agents = reload_agents()
    return {"reloaded": list(agents.keys())}


@app.get("/agents/{role}/variants")
def api_get_variants(role: str):
    """Get language variants for an agent role."""
    try:
        agent = load_agent(role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent role '{role}' not found")
    return {"role": role, "variants": agent.variants}


# ------------------------------------------------
# DREAM API
# ------------------------------------------------

@app.post("/dream")
def api_run_dream():
    """Trigger a manual dream cycle (memory consolidation)."""
    available = set(_url_cache.keys()) if _url_cache else None
    report = run_dream(available_models=available, log_fn=log)
    return report


@app.get("/dream/log")
def api_dream_log():
    """Get dream cycle history."""
    return {"log": dream_load_json(DREAM_LOG, [])}


@app.get("/dream/health")
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
# ------------------------------------------------

@app.get("/gates")
def api_list_gates():
    """List all gate rules with summary stats."""
    return get_gates_summary()


@app.post("/gates")
def api_add_gate(body: dict):
    """Add a manual gate rule."""
    pattern = body.get("pattern")
    reason = body.get("reason")
    if not pattern or not reason:
        raise HTTPException(status_code=400, detail="'pattern' and 'reason' are required")
    severity = body.get("severity", "block")
    gate = add_gate(pattern, reason, source="manual", severity=severity)
    return gate


@app.delete("/gates/{gate_id}")
def api_remove_gate(gate_id: str):
    """Remove a gate rule by ID."""
    remove_gate(gate_id)
    return {"deleted": gate_id}


@app.put("/gates/{gate_id}/toggle")
def api_toggle_gate(gate_id: str, body: dict):
    """Enable or disable a gate rule."""
    enabled = body.get("enabled", True)
    toggle_gate(gate_id, enabled)
    return {"gate_id": gate_id, "enabled": enabled}


@app.get("/gates/lessons")
def api_list_lessons():
    """Get lessons summary with recent incidents."""
    return get_lessons_summary()


@app.post("/gates/consolidate")
def api_consolidate_gates(body: dict = None):
    """Manually trigger lesson consolidation. Pass dry_run=true to preview."""
    dry_run = (body or {}).get("dry_run", False)
    report = consolidate_lessons(dry_run=dry_run, log_fn=log)
    return report


@app.get("/logs/{run_id}/tail")
def tail_log(run_id: str, lines: int = 80):
    """Return the last N lines of a run's log file."""
    log_path = Path(LOG_DIR) / f"{run_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log not found")
    try:
        content = log_path.read_text()
        all_lines = content.strip().splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {
            "run_id": run_id,
            "total_lines": len(all_lines),
            "lines": tail
        }
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/control/pause")
def toggle_pause():
    """Toggle orchestrator pause state. When paused, new runs are rejected."""
    global ORCHESTRATOR_PAUSED
    ORCHESTRATOR_PAUSED = not ORCHESTRATOR_PAUSED
    return {"paused": ORCHESTRATOR_PAUSED}


@app.get("/control/status")
def control_status():
    """Get orchestrator control status."""
    return {
        "paused": ORCHESTRATOR_PAUSED,
        "active_runs": len([r for r in RUN_STATUS.values() if not r.get("completed", True)])
    }


@app.post("/control/restart")
def restart_orchestrator():
    """
    Restart the orchestrator service.
    Returns immediately; the service restarts in the background.
    """
    import subprocess as _sp
    try:
        _sp.Popen(
            ["bash", "-c", "sleep 1 && systemctl restart ai-orchestrator"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
        )
        return {"status": "restarting", "message": "Service will restart in ~1 second"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restart failed: {e}")


# ------------------------------------------------
# WEB UI
# ------------------------------------------------

UI_DIR = Path("/opt/ai-orchestrator/ui")

@app.get("/ui", response_class=HTMLResponse)
def serve_ui():
    """Serve the 3D graph visualization."""
    graph_path = UI_DIR / "graph.html"
    if not graph_path.exists():
        raise HTTPException(status_code=404, detail="UI not found. Place graph.html in /opt/ai-orchestrator/ui/")
    return graph_path.read_text()


@app.get("/ui/{filename}")
def serve_ui_file(filename: str):
    """Serve additional UI files (CSS, JS, etc.)."""
    filepath = UI_DIR / filename
    if not filepath.exists() or ".." in filename:
        raise HTTPException(status_code=404)
    return FileResponse(filepath)


# ------------------------------------------------
# WEBSOCKET: LIVE STREAMING
# ------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Live streaming endpoint. Clients receive JSON messages:
      {"type": "log",    "run_id": "...", "line": "...", "phase": "..."}
      {"type": "status", "run_id": "...", "phase": "...", "score": N, ...}
    """
    await ws.accept()
    with _ws_lock:
        _ws_clients.append(ws)
    try:
        while True:
            # keep connection alive; client can send pings or filter commands
            data = await ws.receive_text()
            # echo back as heartbeat acknowledgement
            await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            try:
                _ws_clients.remove(ws)
            except ValueError:
                pass


# ------------------------------------------------
# API: CONFIG (read / write config.json from UI)
# ------------------------------------------------

@app.get("/config")
def get_config():
    """Return current config.json contents."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Could not read config: {e}")


class ConfigUpdate(BaseModel):
    config: dict


@app.put("/config")
def update_config(req: ConfigUpdate):
    """
    Update config.json. Takes the full config object.
    Backs up current config before writing.
    Returns the saved config. Requires service restart to take effect.
    """
    import copy

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


# ------------------------------------------------
# API: PROJECTS (list all deployed projects across targets)
# ------------------------------------------------

@app.get("/projects/deployed")
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

