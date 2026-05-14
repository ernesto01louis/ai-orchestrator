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
import shlex

from fastapi import (
    APIRouter,
    HTTPException,
)
from pydantic import BaseModel

from core.config import (
    DEPLOY_BASE,
)
from execution import (
    environment_inspector,
    ssh_command,
    validate_target,
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
