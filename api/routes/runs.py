"""Run lifecycle (orchestrate, status, result, runs index, files, verify, resume, intervene, logs, control) routes (carved from api/routes/__init__.py).

Audit Stage 5 §D.1 split. The package-level ``router`` aggregator in
``api/routes/__init__.py`` mounts this sub-module via
``include_router``. Public URL paths and response shapes are
byte-identical to pre-split.

Imports are inherited verbatim from the pre-split preamble — some are
unused in this sub-module; a follow-up commit can trim.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
)

from core.paths import (
    LOG_DIR,
    PROJECTS_DIR,
)
from core.runtime import (
    ORCHESTRATOR_PAUSED,
    RUN_STATUS,
    _init_run_status,
    _load_run_index,
    _run_status_lock,
    _update_run_status,
    log,
)
from execution import (
    validate_target,
)
from manifest import verify_run_manifest
from orchestration import (
    OrchestrateRequest,
)
from prefect_io import (
    submit_orchestration,
)

# Filename safety regex (was at app.py:181 originally)
# UI assets directory served by /ui routes
from ._common import SAFE_FILENAME, UI_DIR  # noqa: F401

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

    if info.get("flow_run_id"):
        response["flow_run_id"] = info["flow_run_id"]

    if info.get("project"):
        response["project"] = info["project"]

    if info.get("target"):
        response["target"] = info["target"]

    if info["completed"]:

        if info["error"]:
            response["error"] = info["error"]

        if info["result"]:
            response["result"] = info["result"]

    # Lazy manifest_status: compute on first read for completed runs.
    manifest_status = info.get("manifest_status")
    if manifest_status is None and info.get("completed") is True:
        project = info.get("project")
        if project:
            run_dir = Path(PROJECTS_DIR) / project / "runs" / run_id
            try:
                result = verify_run_manifest(run_dir)
                manifest_status = result.status
            except Exception as exc:  # noqa: BLE001
                log.warning("lazy manifest verify failed for %s: %s", run_id, exc)
                manifest_status = "skipped"
            RUN_STATUS[run_id]["manifest_status"] = manifest_status

    response["manifest_status"] = manifest_status
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


@router.get("/runs/{run_id}/verify")
def verify_run(run_id: str) -> dict[str, Any]:
    """Force a manifest integrity check for a completed run.

    Re-hashes every tracked artifact against the stored manifest.json
    and returns the result. Updates RUN_STATUS[run_id]["manifest_status"]
    so subsequent GET /status calls see the fresh value.

    Always returns HTTP 200 — mismatches are domain-level, not HTTP errors.
    """
    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    info = RUN_STATUS[run_id]
    project = info.get("project", "")
    run_dir = Path(PROJECTS_DIR) / project / "runs" / run_id

    try:
        result = verify_run_manifest(run_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("verify_run_manifest failed for %s: %s", run_id, exc)
        RUN_STATUS[run_id]["manifest_status"] = "skipped"
        return {
            "run_id": run_id,
            "valid": False,
            "status": "skipped",
            "mismatches": [f"verify failed: {exc}"],
        }

    RUN_STATUS[run_id]["manifest_status"] = result.status

    return {
        "run_id": run_id,
        "valid": result.valid,
        "status": result.status,
        "mismatches": result.mismatches,
    }


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str) -> dict[str, Any]:
    """Phase 3.2 SmartPause unblock route.

    Clears ``RUN_STATUS[run_id]["paused"]`` so the orchestration loop's
    SmartPause polling wakes up and continues. Idempotent — safe to call
    on a run that isn't paused.

    Phase 3.1 adds the richer ``POST /runs/{run_id}/intervene`` with
    approve/reject/edit semantics; ``/resume`` remains the minimal
    unblock for SmartPause-only deployments and as a fallback when ntfy
    action buttons aren't wired.
    """
    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    prior = RUN_STATUS[run_id].get("paused")
    _update_run_status(run_id, paused=None)

    return {
        "run_id": run_id,
        "previously_paused": prior,
        "paused": None,
    }


@router.post("/runs/{run_id}/intervene")
def intervene_run(run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Phase 3.1 HITL intervention route.

    Drains the operator's decision (approve / reject / edit) onto
    ``core.hitl.INTERVENTION_QUEUE[run_id]`` so the blocked
    orchestration loop's ``wait_for_intervention`` returns. Also
    clears ``RUN_STATUS[run_id]["paused"]`` so SmartPause-style
    pollers wake up.

    Body shape::

        {"action": "approve" | "reject" | "edit",
         "prompt": "<override>"     // only required for action=edit
        }

    Returns 404 for unknown run_id, 400 for an invalid action.
    """
    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    action = (body or {}).get("action")
    if action not in ("approve", "reject", "edit"):
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of approve|reject|edit, got: {action!r}",
        )

    payload = {"action": action}
    if action == "edit":
        prompt = (body or {}).get("prompt")
        if not prompt or not isinstance(prompt, str):
            raise HTTPException(
                status_code=400,
                detail="action=edit requires a non-empty 'prompt' field",
            )
        payload["prompt"] = prompt

    from core.hitl import post_intervention

    try:
        post_intervention(run_id, payload)
    except Exception as exc:  # noqa: BLE001 — propagate as 409
        raise HTTPException(
            status_code=409,
            detail=f"intervention queue full or unavailable: {exc}",
        ) from exc

    # Clear the paused flag so SmartPause's polling loop also unblocks
    # for runs that were SmartPaused before HITL took over.
    _update_run_status(run_id, paused=None)

    return {
        "run_id": run_id,
        "action": action,
        "queued": True,
    }


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
