"""References (RAG corpus) + vault + graph-data routes (carved from api/routes/__init__.py).

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
