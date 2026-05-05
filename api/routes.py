"""HTTP + WebSocket route handlers.

All 82 routes live on a single APIRouter for now. Plan calls for splitting
by area (api/runs.py, api/memory.py, api/vault.py, etc.) — defer that
sub-split to a follow-up; collapsing app.py's monolith into one router
file is the immediate Phase 0 win.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from datetime import datetime
from pathlib import Path

import requests
from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import llm.ollama as _llm_ollama
from agents.loader import load_agent, load_all_agents
from agents.loader import reload_all as reload_agents
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
from prefect_io import (  # noqa: F401
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
SAFE_FILENAME = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]+$")

# UI assets directory served by /ui routes
UI_DIR = Path("/opt/ai-orchestrator/ui")


router = APIRouter()


@router.post("/orchestrate")
def orchestrate(req: OrchestrateRequest):

    if ORCHESTRATOR_PAUSED:
        raise HTTPException(status_code=503, detail="Orchestrator is paused")

    try:
        validate_target(req.deploy_target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = str(uuid.uuid4())

    _init_run_status(run_id, project=req.project_name, target=req.deploy_target)

    result = submit_orchestration(req, run_id)

    # Keep flow_run_id alongside RUN_STATUS so pause/resume/cancel can target it.
    with _run_status_lock:
        if run_id in RUN_STATUS:
            RUN_STATUS[run_id]["flow_run_id"] = result["flow_run_id"]

    return {
        "run_id": run_id,
        "flow_run_id": result["flow_run_id"],
        "status": "started",
        "poll": f"/status/{run_id}",
    }


@router.get("/status/{run_id}")
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


@router.get("/result/{run_id}")
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


@router.get("/runs")
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


@router.get("/files/{run_id}")
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


@router.get("/environment/{target}")
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


# ------------------------------------------------
# API: RUN A DEPLOYED PROJECT
# ------------------------------------------------

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


# ------------------------------------------------
# API: DELETE A DEPLOYED PROJECT
# ------------------------------------------------

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


# ------------------------------------------------
# API: MEMORY ENDPOINTS
# ------------------------------------------------

@router.get("/memory")
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


@router.get("/memory/negative")
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


@router.get("/memory/search")
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


@router.get("/model-stats")
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


@router.get("/model-stats/{model_name}")
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

@router.get("/briefing")
def get_briefing():
    """
    Full status briefing combining all memory layers.
    Use this after returning from a development gap.
    """
    return build_briefing()


# ------------------------------------------------
# API: IDENTITY (Layer 1)
# ------------------------------------------------

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


# ------------------------------------------------
# API: PRIMER (Layer 2)
# ------------------------------------------------

@router.get("/primer")
def get_primer():
    """View the current primer.md (session state)."""
    return {"content": load_primer()}


# ------------------------------------------------
# API: GOALS
# ------------------------------------------------

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


# ------------------------------------------------
# API: SESSIONS
# ------------------------------------------------

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


# ------------------------------------------------
# API: LIVE CONTEXT (Layer 3)
# ------------------------------------------------

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


# ------------------------------------------------
# API: OLLAMA MODELS
# ------------------------------------------------

@router.get("/models")
def get_models():
    """List all available and currently loaded models across both Ollama servers."""
    return {
        "loaded": get_loaded_models(),
        "available": get_available_models()
    }


@router.get("/models/loaded")
def get_models_loaded():
    """Quick check: which models are currently hot in memory."""
    return {"loaded": get_loaded_models()}


# ------------------------------------------------
# API: SYSTEM HEALTH
# ------------------------------------------------

@router.get("/health")
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

@router.get("/hindsight/status")
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


@router.post("/hindsight/recall")
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


@router.post("/hindsight/retain")
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


@router.post("/hindsight/reflect")
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

@router.get("/hindsight/mental-models")
def api_mental_models():
    """List all Hindsight mental models with their content."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    models = hindsight_get_mental_models("api")
    return {"models": models}


@router.post("/hindsight/mental-models/{model_id}/refresh")
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



@router.post("/references/upload")
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


@router.get("/references")
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


@router.get("/references/{filename}/content")
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


@router.delete("/references/{filename}")
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

@router.get("/graph-data")
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


# ------------------------------------------------
# API: NOTIFICATIONS
# ------------------------------------------------

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




# ------------------------------------------------
# API: VAULT (L5)
# ------------------------------------------------

@router.get("/vault/status")
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


@router.post("/vault/sync")
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


@router.post("/vault/sync-nas")
def vault_force_sync_nas():
    """Force sync vault to NAS mount point."""
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")
    if not VAULT_NAS_ENABLED:
        raise HTTPException(status_code=503, detail="NAS sync is disabled")

    ok = vault_sync_to_nas("api-vault-nas")
    return {"status": "synced" if ok else "failed", "path": VAULT_NAS_PATH}


@router.post("/vault/rebuild")
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


@router.get("/vault/note/{subdir}/{filename}")
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


@router.post("/hindsight/reflect/auto")
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
# ------------------------------------------------

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
def api_reload_agents():
    """Force-reload all agent configs from disk."""
    agents = reload_agents()
    return {"reloaded": list(agents.keys())}


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
# ------------------------------------------------

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
# ------------------------------------------------

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


@router.get("/logs/{run_id}/tail")
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


@router.post("/control/pause")
def toggle_pause():
    """Toggle orchestrator pause state. When paused, new runs are rejected."""
    global ORCHESTRATOR_PAUSED
    ORCHESTRATOR_PAUSED = not ORCHESTRATOR_PAUSED
    return {"paused": ORCHESTRATOR_PAUSED}


@router.get("/control/status")
def control_status():
    """Get orchestrator control status."""
    return {
        "paused": ORCHESTRATOR_PAUSED,
        "active_runs": len([r for r in RUN_STATUS.values() if not r.get("completed", True)])
    }


@router.post("/control/restart")
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

@router.websocket("/ws")
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
            await ws.receive_text()
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


# ------------------------------------------------
# API: PROJECTS (list all deployed projects across targets)
# ------------------------------------------------

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


# ------------------------------------------------
# API: CAMPAIGNS (Phase 1.1)
# ------------------------------------------------

def _campaign_or_404(campaign_id: str) -> dict:
    campaigns = load_campaigns()
    if campaign_id not in campaigns:
        raise HTTPException(status_code=404, detail=f"Unknown campaign_id: {campaign_id}")
    return campaigns[campaign_id]


def _set_campaign_flag(campaign_id: str, flag: str, value: bool) -> None:
    with _campaign_status_lock:
        cs = CAMPAIGN_STATUS.setdefault(campaign_id, {})
        cs[flag] = value


@router.post("/campaigns")
def create_campaign(req: CampaignCreate):
    """Create a campaign and start its runner thread.

    Mirrors /orchestrate: returns immediately with campaign_id; client
    polls /campaigns/{id} or /campaigns/{id}/tree for progress.
    """
    if ORCHESTRATOR_PAUSED:
        raise HTTPException(status_code=503, detail="Orchestrator is paused")

    try:
        validate_target(req.template.deploy_target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Lazy import to avoid circular at module load.
    from orchestration.campaign import expand_grid

    campaign_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    # Validate the grid is expandable up front so we can return run_count.
    combos = expand_grid(req.params, max_runs=req.max_runs)

    record = {
        **req.model_dump(),
        "id": campaign_id,
        "status": "queued",
        "runs": [],
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    campaigns = load_campaigns()
    campaigns[campaign_id] = record
    save_campaigns(campaigns)

    with _campaign_status_lock:
        CAMPAIGN_STATUS[campaign_id] = {
            "phase": "queued", "paused": False, "aborted": False,
            "current_run_id": None,
        }

    result = submit_campaign(campaign_id)

    with _campaign_status_lock:
        if campaign_id in CAMPAIGN_STATUS:
            CAMPAIGN_STATUS[campaign_id]["flow_run_id"] = result["flow_run_id"]

    return {
        "campaign_id": campaign_id,
        "flow_run_id": result["flow_run_id"],
        "run_count": len(combos),
        "status": "started",
        "poll": f"/campaigns/{campaign_id}",
    }


@router.get("/campaigns")
def list_campaigns():
    """List all campaigns with summary fields (id, name, status, run_count, mean_score)."""
    campaigns = load_campaigns()
    out = []
    for cid, c in campaigns.items():
        runs = c.get("runs", [])
        scores = [r.get("score", 0) for r in runs if r.get("score") is not None]
        mean = sum(scores) / len(scores) if scores else None
        out.append({
            "id": cid,
            "name": c.get("name"),
            "status": c.get("status"),
            "run_count": len(runs),
            "mean_score": mean,
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        })
    return {"campaigns": out}


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    """Full campaign record."""
    return _campaign_or_404(campaign_id)


@router.get("/campaigns/{campaign_id}/tree")
def get_campaign_tree(campaign_id: str):
    """Tree view: campaign + per-run children with live phase merged in."""
    campaign = _campaign_or_404(campaign_id)
    runs_out = []
    for r in campaign.get("runs", []):
        rid = r["run_id"]
        live = RUN_STATUS.get(rid)
        if live:
            phase = live.get("phase", r.get("status"))
            score = live.get("score", r.get("score"))
            completed = live.get("completed", False)
        else:
            phase = r.get("status")
            score = r.get("score")
            completed = r.get("status") in ("completed", "failed")
        runs_out.append({
            "run_id": rid,
            "params": r.get("params", {}),
            "phase": phase,
            "score": score,
            "completed": completed,
        })
    return {"campaign": campaign, "runs": runs_out}


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: str):
    _campaign_or_404(campaign_id)
    _set_campaign_flag(campaign_id, "paused", True)
    return {"campaign_id": campaign_id, "paused": True}


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: str):
    _campaign_or_404(campaign_id)
    _set_campaign_flag(campaign_id, "paused", False)
    return {"campaign_id": campaign_id, "paused": False}


@router.post("/campaigns/{campaign_id}/abort")
def abort_campaign(campaign_id: str):
    """Best-effort abort: stops new runs from spawning. Does NOT interrupt
    an in-flight orchestrator run — matches existing global-pause semantics.
    """
    _campaign_or_404(campaign_id)
    _set_campaign_flag(campaign_id, "aborted", True)
    return {"campaign_id": campaign_id, "aborted": True}


# ------------------------------------------------
# API: EVIDENCE BUNDLES (Phase 1.2)
# ------------------------------------------------


def _crate_dir(campaign_id: str) -> Path:
    """Filesystem location of the campaign's evidence crate."""
    from evidence.builder import CAMPAIGNS_OUTPUT_DIR

    return CAMPAIGNS_OUTPUT_DIR / campaign_id


def _crate_or_404(campaign_id: str) -> Path:
    _campaign_or_404(campaign_id)
    crate = _crate_dir(campaign_id)
    if not (crate / "evidence.json").exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"No evidence bundle for campaign {campaign_id} yet. "
                "Bundles are emitted after each run; trigger a refresh "
                "with POST /campaigns/{id}/evidence/refresh if needed."
            ),
        )
    return crate


@router.get("/campaigns/{campaign_id}/evidence")
def get_evidence(campaign_id: str):
    """Return the EvidenceBundle JSON for a campaign."""
    crate = _crate_or_404(campaign_id)
    return json.loads((crate / "evidence.json").read_text())


@router.get("/campaigns/{campaign_id}/evidence.crate.zip")
def get_evidence_crate_zip(campaign_id: str):
    """Stream the entire RO-Crate directory as a zip.

    Suitable for ``curl -O`` + ``unzip`` — the unzipped tree is a
    standalone bundle that the standalone verifier can run against.
    """
    import io
    import zipfile

    from fastapi.responses import Response

    crate = _crate_or_404(campaign_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(crate.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(crate)))
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="campaign-{campaign_id[:8]}.crate.zip"'
            ),
        },
    )


@router.get("/campaigns/{campaign_id}/evidence/verify")
def verify_evidence(campaign_id: str):
    """Recompute artifact digests + verify the DSSE envelope signature.

    Pure verification path: no signing-key access needed, only the
    public key embedded in the crate. Returns ``{valid, errors}``.
    """
    import base64

    from core.evidence import DsseEnvelope, EvidenceBundle, InTotoStatement
    from evidence.signing import sha256_file, verify_envelope

    crate = _crate_or_404(campaign_id)
    errors: list[str] = []

    manifest_path = crate / "manifest.json"
    dsse_path = crate / "manifest.json.dsse"
    public_key_path = crate / "public.key"
    evidence_path = crate / "evidence.json"

    for required in (manifest_path, dsse_path, public_key_path, evidence_path):
        if not required.exists():
            errors.append(f"missing: {required.name}")

    if errors:
        return {"valid": False, "errors": errors}

    statement = InTotoStatement.model_validate_json(manifest_path.read_text())
    for subj in statement.subject:
        target = crate / subj.name
        if not target.exists():
            errors.append(f"manifest references missing file: {subj.name}")
            continue
        actual = sha256_file(target)
        expected = subj.digest["sha256"]
        if actual != expected:
            errors.append(
                f"sha256 mismatch on {subj.name}: "
                f"expected {expected[:12]}…, got {actual[:12]}…"
            )

    envelope = DsseEnvelope.model_validate_json(dsse_path.read_text())
    public_key = base64.b64decode(public_key_path.read_text().strip())
    if not verify_envelope(envelope, public_key):
        errors.append("DSSE envelope signature did not verify")

    try:
        EvidenceBundle.model_validate_json(evidence_path.read_text())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"evidence.json failed schema validation: {exc}")

    return {
        "valid": not errors,
        "errors": errors,
        "subject_count": len(statement.subject),
        "keyid": envelope.signatures[0].keyid if envelope.signatures else None,
    }


@router.post("/campaigns/{campaign_id}/evidence/refresh")
def refresh_evidence(campaign_id: str):
    """Force a rebuild of the evidence bundle for a campaign.

    Useful after a calculator plugin lands or a run completes outside
    the normal post-run hook (e.g., rebuilding historical campaigns).
    Requires the signing key on disk; in-memory keys are test-only.
    """
    _campaign_or_404(campaign_id)

    try:
        from evidence.builder import build_bundle

        bundle = build_bundle(campaign_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Signing key unavailable: {exc}. Run "
                "scripts/install_signing_key.sh on the orchestrator host."
            ),
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"build_bundle failed: {exc}") from exc

    return {
        "campaign_id": campaign_id,
        "bundle_id": bundle.bundle_id,
        "artifact_count": len(bundle.artifacts),
        "calculator_count": len(bundle.calculators),
    }

