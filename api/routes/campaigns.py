"""Campaign + evidence bundle + budget + tree + pause/resume/abort + Merkle verify routes (carved from api/routes/__init__.py).

Audit Stage 5 §D.1 split. The package-level ``router`` aggregator in
``api/routes/__init__.py`` mounts this sub-module via
``include_router``. Public URL paths and response shapes are
byte-identical to pre-split.

Imports are inherited verbatim from the pre-split preamble — some are
unused in this sub-module; a follow-up commit can trim.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
)
from fastapi.responses import Response

from core.campaign import CampaignCreate
from core.paths import (
    CAMPAIGN_TEMPLATES_DIR,
    PROJECTS_DIR,
)
from core.runtime import (
    CAMPAIGN_STATUS,
    ORCHESTRATOR_PAUSED,
    RUN_STATUS,
    _campaign_status_lock,
    log,
)
from execution import (
    validate_target,
)
from manifest import verify_campaign_merkle
from memory_pkg import (
    load_campaigns,
    save_campaigns,
)
from prefect_io import (
    cancel_flow_run,
    pause_flow_run,
    resume_flow_run,
    submit_campaign,
)

# Filename safety regex (was at app.py:181 originally)
# UI assets directory served by /ui routes
from ._common import SAFE_FILENAME, UI_DIR  # noqa: F401

router = APIRouter()


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
    save_campaigns(campaigns, changed_ids={campaign_id})

    with _campaign_status_lock:
        CAMPAIGN_STATUS[campaign_id] = {
            "phase": "queued", "paused": False, "aborted": False,
            "current_run_id": None, "manifest_status": None,
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


@router.get("/campaigns/{campaign_id}/budget")
def get_campaign_budget(campaign_id: str):
    """Phase 2.4 budget summary for a campaign.

    Returns the running ``budget_used_usd``, the operator-set
    ``budget_total_usd`` (or ``None`` for unlimited), the percentage
    consumed (or ``None`` when no total is set), the ``budget_state``,
    and the list of percentage thresholds that have already fired
    notifications. ``budget.enabled`` from config is reflected so
    operators don't have to look in two places to know whether
    accruals are happening.
    """
    from core import budget as _budget  # noqa: PLC0415
    from core import config as _config  # noqa: PLC0415

    campaign = _campaign_or_404(campaign_id)
    used = float(campaign.get("budget_used_usd", 0.0) or 0.0)
    total_raw = campaign.get("budget_total_usd")
    total: float | None = float(total_raw) if total_raw is not None else None
    return {
        "campaign_id": campaign_id,
        "enabled": bool(_config.BUDGET_ENABLED),
        "budget_used_usd": used,
        "budget_total_usd": total,
        "percentage_used": _budget.percentage_used(used, total),
        "budget_state": str(campaign.get("budget_state", "ok") or "ok"),
        "thresholds_emitted": list(
            campaign.get("budget_thresholds_emitted", []) or []
        ),
        "thresholds_pct": list(_config.BUDGET_THRESHOLDS_PCT),
    }


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
    flow_run_id = CAMPAIGN_STATUS.get(campaign_id, {}).get("flow_run_id")
    pause_flow_run(flow_run_id)
    return {"campaign_id": campaign_id, "paused": True, "flow_run_id": flow_run_id}


@router.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: str):
    _campaign_or_404(campaign_id)
    _set_campaign_flag(campaign_id, "paused", False)
    flow_run_id = CAMPAIGN_STATUS.get(campaign_id, {}).get("flow_run_id")
    resume_flow_run(flow_run_id)
    return {"campaign_id": campaign_id, "paused": False, "flow_run_id": flow_run_id}


@router.post("/campaigns/{campaign_id}/abort")
def abort_campaign(campaign_id: str):
    """Best-effort abort: stops new runs from spawning. Does NOT interrupt
    an in-flight orchestrator run — matches existing global-pause semantics.
    """
    _campaign_or_404(campaign_id)
    _set_campaign_flag(campaign_id, "aborted", True)
    flow_run_id = CAMPAIGN_STATUS.get(campaign_id, {}).get("flow_run_id")
    cancel_flow_run(flow_run_id)
    return {"campaign_id": campaign_id, "aborted": True, "flow_run_id": flow_run_id}


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


@router.get("/campaigns/{campaign_id}/verify-merkle")
def verify_campaign_merkle_route(campaign_id: str) -> dict[str, Any]:
    """Re-validate the campaign Merkle root against all per-run manifest.json files.

    Re-hashes every run's manifest.json and rebuilds the Merkle tree to compare
    against the stored root in merkle.json. Updates
    CAMPAIGN_STATUS[campaign_id]["manifest_status"] with the result.

    Always returns HTTP 200 — mismatches are domain-level, not HTTP errors.
    """
    _campaign_or_404(campaign_id)

    campaign_dir = CAMPAIGN_TEMPLATES_DIR / campaign_id
    projects_root = Path(PROJECTS_DIR)

    try:
        result = verify_campaign_merkle(campaign_dir, projects_root)
    except Exception as exc:  # noqa: BLE001
        log.warning("verify_campaign_merkle failed for %s: %s", campaign_id, exc)
        with _campaign_status_lock:
            CAMPAIGN_STATUS.setdefault(campaign_id, {})["manifest_status"] = "skipped"
        return {
            "campaign_id": campaign_id,
            "valid": False,
            "status": "skipped",
            "mismatches": [f"verify failed: {exc}"],
        }

    with _campaign_status_lock:
        CAMPAIGN_STATUS.setdefault(campaign_id, {})["manifest_status"] = result.status

    return {
        "campaign_id": campaign_id,
        "valid": result.valid,
        "status": result.status,
        "mismatches": result.mismatches,
    }
