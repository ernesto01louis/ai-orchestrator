"""Campaign runner (Phase 1.1).

Daemon-thread worker that expands a parameter grid and feeds each combo
to ``run_orchestration`` sequentially, respecting per-campaign pause and
abort flags. Mirrors the existing run-thread shape: write to the live
``CAMPAIGN_STATUS`` dict, persist durable state via load/save_campaigns.

Sequential by design in Phase 1.1 (parallelism field reserved for
Phase 1.3 Prefect integration). Pause/abort are best-effort and never
interrupt an in-flight orchestrator run — they only gate spawn of the
*next* run, matching the existing global-pause semantics.
"""
from __future__ import annotations

import itertools
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from prefect import flow, task

from core.paths import CAMPAIGN_TEMPLATES_DIR, PROJECTS_DIR
from core.runtime import (
    CAMPAIGN_STATUS,
    RUN_STATUS,
    _campaign_status_lock,
    _init_run_status,
    log,
)
from manifest import write_campaign_merkle
from memory_pkg import load_campaigns, save_campaigns, vault_write_campaign_note
from prefect_io.state_hooks import (
    on_cancelled,
    on_completion,
    on_failure,
    on_running,
)

_PAUSE_POLL_SECONDS = 5.0


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat()


def expand_grid(
    params_grid: dict[str, list], max_runs: int | None = None
) -> list[dict[str, Any]]:
    """Cartesian product of params_grid → list of param dicts.

    Empty grid → one combo with no params (a single run).
    Optional ``max_runs`` truncates from the front.
    """
    if not params_grid:
        return [{}]
    keys = list(params_grid.keys())
    values = [list(params_grid[k]) for k in keys]
    combos = [dict(zip(keys, c)) for c in itertools.product(*values)]
    if max_runs is not None and max_runs >= 0:
        combos = combos[:max_runs]
    return combos


def materialize_template(template: dict, params: dict) -> dict:
    """Apply {param} substitution to every string field in a template dict.

    Non-string fields and lists-of-non-strings pass through. List of
    strings (e.g. generator_models) are substituted element-wise.
    Substitution failures (KeyError on a missing param) leave the field
    unchanged — params are advisory placeholders, not strict requirements.
    """
    out: dict = {}
    for k, v in template.items():
        if isinstance(v, str):
            out[k] = _safe_format(v, params)
        elif isinstance(v, list):
            out[k] = [_safe_format(x, params) if isinstance(x, str) else x for x in v]
        else:
            out[k] = v
    return out


def _safe_format(s: str, params: dict) -> str:
    if "{" not in s:
        return s
    try:
        return s.format(**params)
    except (KeyError, IndexError, ValueError):
        return s


def _set_campaign_phase(campaign_id: str, phase: str) -> None:
    with _campaign_status_lock:
        cs = CAMPAIGN_STATUS.setdefault(campaign_id, {})
        cs["phase"] = phase


def _is_aborted(campaign_id: str) -> bool:
    with _campaign_status_lock:
        return bool(CAMPAIGN_STATUS.get(campaign_id, {}).get("aborted"))


def _is_paused(campaign_id: str) -> bool:
    with _campaign_status_lock:
        return bool(CAMPAIGN_STATUS.get(campaign_id, {}).get("paused"))


@flow(
    name="campaign",
    retries=0,
    on_running=[on_running],
    on_completion=[on_completion],
    on_failure=[on_failure],
    on_cancellation=[on_cancelled],
)
def run_campaign(campaign_id: str) -> None:
    """Daemon-thread entry. Sequentially expands the grid and runs each
    combo through ``run_orchestration``, persisting per-run records into
    the campaign and updating live ``CAMPAIGN_STATUS``.
    """
    # Lazy import to avoid circular import (orchestration imports memory_pkg
    # which imports core.runtime; we want orchestration.campaign to be a
    # leaf module loaded at route-handler time).
    from orchestration import OrchestrateRequest, run_orchestration

    campaigns = load_campaigns()
    if campaign_id not in campaigns:
        return

    _set_campaign_phase(campaign_id, "running")
    campaigns[campaign_id]["status"] = "running"
    campaigns[campaign_id]["updated_at"] = _utcnow_iso()
    save_campaigns(campaigns)
    _safe_vault_write(campaigns[campaign_id])

    template = campaigns[campaign_id]["template"]
    params_grid = campaigns[campaign_id].get("params", {})
    max_runs = campaigns[campaign_id].get("max_runs")
    combos = expand_grid(params_grid, max_runs=max_runs)

    final_status = "completed"

    for combo in combos:
        # Honor pause/abort BEFORE spawning the next run.
        while True:
            if _is_aborted(campaign_id):
                final_status = "aborted"
                break
            if not _is_paused(campaign_id):
                break
            _set_campaign_phase(campaign_id, "paused")
            time.sleep(_PAUSE_POLL_SECONDS)

        if final_status == "aborted":
            break

        _set_campaign_phase(campaign_id, "running")

        run_id = str(uuid.uuid4())
        req_dict = materialize_template(template, combo)

        try:
            req = OrchestrateRequest(**req_dict)
        except Exception as e:
            _record_run(campaign_id, run_id, combo, status="failed", score=0,
                        error=f"template error: {e}")
            continue

        with _campaign_status_lock:
            CAMPAIGN_STATUS.setdefault(campaign_id, {})["current_run_id"] = run_id

        _init_run_status(
            run_id,
            project=req.project_name,
            target=req.deploy_target,
            campaign_id=campaign_id,
        )

        try:
            run_orchestration.with_options(name=f"orchestrate-{run_id[:8]}")(req, run_id)
        except Exception as e:
            log(run_id, f"[campaign {campaign_id}] run crashed: {e}\n{traceback.format_exc()}")

        run_info = RUN_STATUS.get(run_id, {}) or {}
        score = run_info.get("score") or 0
        status = "failed" if run_info.get("error") else "completed"
        _record_run(campaign_id, run_id, combo, status=status, score=score)

    # Finalize.
    campaigns = load_campaigns()
    if campaign_id in campaigns:
        campaigns[campaign_id]["status"] = final_status
        campaigns[campaign_id]["completed_at"] = _utcnow_iso()
        campaigns[campaign_id]["updated_at"] = _utcnow_iso()
        save_campaigns(campaigns)
        _safe_vault_write(campaigns[campaign_id])
        _safe_emit_evidence.submit(campaign_id).result(raise_on_failure=False)

    # Phase C: write campaign-level Merkle root on the success path only.
    if final_status == "completed":
        try:
            campaign_dir = CAMPAIGN_TEMPLATES_DIR / campaign_id
            campaign_dir.mkdir(parents=True, exist_ok=True)
            # Build (run_id, project_name, run_dir) tuples from completed runs.
            run_tuples: list[tuple[str, str, object]] = []
            for run_entry in (campaigns.get(campaign_id, {}).get("runs") or []):
                rid = run_entry.get("run_id", "")
                project = RUN_STATUS.get(rid, {}).get("project") or ""
                if rid and project:
                    run_dir = Path(PROJECTS_DIR) / project / "runs" / rid
                    run_tuples.append((rid, project, run_dir))
            write_campaign_merkle(campaign_dir, run_tuples, campaign_id=campaign_id)
            with _campaign_status_lock:
                CAMPAIGN_STATUS.setdefault(campaign_id, {})["manifest_status"] = "ok"
            log(campaign_id, "manifest: campaign Merkle root written")
        except Exception as exc:
            log(campaign_id, f"campaign merkle write failed (non-fatal): {exc}")
            with _campaign_status_lock:
                CAMPAIGN_STATUS.setdefault(campaign_id, {})["manifest_status"] = "skipped"

    _set_campaign_phase(campaign_id, final_status)
    with _campaign_status_lock:
        cs = CAMPAIGN_STATUS.setdefault(campaign_id, {})
        cs["current_run_id"] = None


def _safe_vault_write(campaign: dict) -> None:
    """Vault writes are best-effort and never raise into the runner."""
    try:
        vault_write_campaign_note(campaign)
    except Exception:
        pass


@task(name="emit_evidence", retries=2)
def _safe_emit_evidence(campaign_id: str) -> None:
    """Best-effort evidence-bundle emission; never raises into the runner.

    Same semantic contract as ``_safe_vault_write``: any failure is
    swallowed so a transient signing-key absence (or a calculator
    plugin crash) doesn't kill the campaign. Real failures are surfaced
    on the next ``GET /campaigns/{id}/evidence/verify`` call.
    """
    try:
        from evidence.builder import build_bundle  # lazy: avoids import cycles
        build_bundle(campaign_id)
    except Exception:
        pass


def _record_run(
    campaign_id: str, run_id: str, params: dict,
    status: str, score: float, error: str | None = None,
) -> None:
    campaigns = load_campaigns()
    if campaign_id not in campaigns:
        return
    entry = {"run_id": run_id, "params": params, "status": status, "score": score}
    if error:
        entry["error"] = error
    campaigns[campaign_id].setdefault("runs", []).append(entry)
    campaigns[campaign_id]["updated_at"] = _utcnow_iso()
    save_campaigns(campaigns)
    _safe_vault_write(campaigns[campaign_id])
    _safe_emit_evidence.submit(campaign_id).result(raise_on_failure=False)
