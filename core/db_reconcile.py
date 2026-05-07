"""Phase 2.1 reconcile-on-startup.

After ``alembic upgrade head`` lays out the schema and the operator
flips ``postgres.enabled=true``, the orchestrator starts and finds an
empty Postgres alongside JSON files that already have months of state.
``reconcile_all`` sweeps the JSON canonical store, upserts each
record into Postgres (idempotent — re-runs are no-ops), and emits a
single structured log line summarising what landed.

Invariants:

* JSON wins on conflict. Reconcile is "import what we have", not
  "merge with what's already there".
* Idempotent: every DAO uses ``ON CONFLICT … DO UPDATE`` (or ``DO
  NOTHING``), so re-running has no extra effect beyond updating
  ``updated_at``-style fields.
* Skipped entirely when ``postgres.enabled=false``.
* Synchronous — called from the async lifespan via
  ``asyncio.to_thread(reconcile_all)``.

Reconcile steps:

1. ``memory/run_index.json`` → upsert each run.
2. ``memory/campaigns.json`` → upsert each campaign.
3. ``campaigns/<id>/manifest.json.dsse`` (one per campaign that has a
   built bundle) → INSERT IF NOT EXISTS into evidence_bundles, with
   crate_sha256 = sha256 of the envelope file.
4. ``memory/model_stats.json`` → for each model, insert ONE
   ``source='reconcile_seed'`` row dated today (carrying the global
   aggregate counters). Live updates after this point append normal
   ``source='live'`` rows for subsequent dates.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import db, db_models, db_writethrough
from core.paths import CAMPAIGNS_FILE, MODEL_STATS, RUN_INDEX_FILE

log = logging.getLogger(__name__)

# Resolved at call time so tests can patch.
_BUNDLE_DIR_ENV = "AI_ORCHESTRATOR_CAMPAIGNS_DIR"


def _resolve_campaigns_dir() -> Path:
    """Where per-campaign RO-Crates live. Imports from evidence.builder
    lazily so the reconcile module doesn't drag the evidence subsystem
    onto every import path."""
    from evidence.builder import CAMPAIGNS_OUTPUT_DIR
    return CAMPAIGNS_OUTPUT_DIR


def _safe_load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "reconcile_json_unreadable",
            extra={"path": str(path), "error": repr(exc)},
        )
        return default


def _parse_iso_or_now(value: Any) -> datetime:
    parsed = db_writethrough._parse_iso(value)
    return parsed if parsed is not None else datetime.now(UTC)


# ---------------------------------------------------------------------------
# Per-table reconcile steps
# ---------------------------------------------------------------------------


def _reconcile_runs(session: Any, run_index: dict[str, dict[str, Any]]) -> int:
    rows = 0
    for run_id, record in run_index.items():
        err = record.get("error_msg")
        snapshot_like = {
            "phase": record.get("phase", "completed"),
            "score": record.get("score", 0),
            "project": record.get("project", ""),
            "target": record.get("target", ""),
            "error": err,
        }
        row = db_writethrough._run_snapshot_to_row(run_id, snapshot_like)
        # Override completed_at with the JSON timestamp (reconcile uses
        # historical truth, not "now").
        ts = db_writethrough._parse_iso(record.get("timestamp"))
        if ts is not None:
            row["completed_at"] = ts
        # Reconcile doesn't know whether the run had an error_msg
        # truncation — pass error_msg through as-is.
        row["error_msg"] = err if err else row.get("error_msg")
        try:
            db_models.upsert_run(session, row)
            rows += 1
        except Exception as exc:
            log.warning(
                "reconcile_row_failed",
                extra={"table": "runs", "run_id": run_id, "error": repr(exc)},
            )
    return rows


def _reconcile_campaigns(
    session: Any, campaigns_map: dict[str, dict[str, Any]]
) -> int:
    rows = 0
    for cid, record in campaigns_map.items():
        row = db_writethrough._campaign_record_to_row(cid, record)
        try:
            db_models.upsert_campaign(session, row)
            rows += 1
        except Exception as exc:
            log.warning(
                "reconcile_row_failed",
                extra={"table": "campaigns", "campaign_id": cid, "error": repr(exc)},
            )
    return rows


def _campaign_dirs_with_envelopes(root: Path) -> Iterable[tuple[Path, Path]]:
    """Yield (campaign_dir, envelope_path) for each campaign whose
    crate has a signed manifest.json.dsse."""
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        envelope = entry / "manifest.json.dsse"
        if envelope.is_file():
            yield entry, envelope


def _reconcile_evidence_bundles(session: Any, root: Path) -> int:
    from evidence.signing import sha256_file
    rows = 0
    for crate_dir, envelope_path in _campaign_dirs_with_envelopes(root):
        evidence_path = crate_dir / "evidence.json"
        bundle_meta = _safe_load_json(evidence_path, default=None)
        if not isinstance(bundle_meta, dict):
            continue
        bundle_id = bundle_meta.get("bundle_id")
        if not bundle_id:
            continue
        try:
            crate_sha256 = sha256_file(envelope_path)
        except OSError as exc:
            log.warning(
                "reconcile_envelope_unreadable",
                extra={"path": str(envelope_path), "error": repr(exc)},
            )
            continue
        row = {
            "bundle_id": bundle_id,
            "campaign_id": bundle_meta.get("campaign_id") or crate_dir.name,
            "schema_version": bundle_meta.get("schema_version", "1.0.0"),
            "crate_path": str(crate_dir),
            "crate_sha256": crate_sha256,
            "created_at": _parse_iso_or_now(bundle_meta.get("created_at")),
            "signed_by_keyid": None,
        }
        try:
            db_models.insert_evidence_bundle(session, row)
            rows += 1
        except Exception as exc:
            log.warning(
                "reconcile_row_failed",
                extra={
                    "table": "evidence_bundles",
                    "bundle_id": bundle_id,
                    "error": repr(exc),
                },
            )
    return rows


def _reconcile_model_stats_seed(
    session: Any, model_stats: dict[str, dict[str, Any]]
) -> int:
    """Insert one synthetic 'reconcile_seed' row per model (today's date)
    carrying the global aggregate counters. Subsequent live updates
    create new rows for new dates with source='live'.

    Idempotent: ON CONFLICT (model_name, date) DO UPDATE will *increment*
    if re-run — but reconcile only runs at startup, before any live
    activity for the same date, so the seed lands once and stays put.
    """
    rows = 0
    today = datetime.now(UTC).date()
    now = datetime.now(UTC)
    for model, s in model_stats.items():
        if not isinstance(s, dict):
            continue
        try:
            db_models.upsert_model_stats_daily(
                session,
                {
                    "model_name": model,
                    "date": today,
                    "runs": int(s.get("total_runs", 0) or 0),
                    "total_score": float(s.get("total_score", 0) or 0),
                    "wins": int(s.get("wins", 0) or 0),
                    "failures": int(s.get("failures", 0) or 0),
                    "by_language": s.get("by_language", {}) or {},
                    "by_role": s.get("by_role", {}) or {},
                    "by_project_type": s.get("by_project_type", {}) or {},
                    "source": "reconcile_seed",
                    "updated_at": now,
                },
            )
            rows += 1
        except Exception as exc:
            log.warning(
                "reconcile_row_failed",
                extra={"table": "model_stats_daily", "model": model, "error": repr(exc)},
            )
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def reconcile_all() -> dict[str, Any]:
    """Sweep JSON canonical state into Postgres. Returns a dict with
    counts (handy for tests and structured logging).

    No-ops when Postgres is disabled."""
    if not db.is_enabled():
        log.info("reconcile_skipped postgres_disabled")
        return {"skipped": True}

    started = time.monotonic()
    counts: dict[str, int] = {
        "runs": 0,
        "campaigns": 0,
        "evidence_bundles": 0,
        "model_stats_seed_rows": 0,
    }

    run_index = _safe_load_json(RUN_INDEX_FILE, default={})
    campaigns_map = _safe_load_json(CAMPAIGNS_FILE, default={})
    model_stats = _safe_load_json(MODEL_STATS, default={})
    campaigns_dir = _resolve_campaigns_dir()

    try:
        with db.get_session() as session:
            counts["runs"] = _reconcile_runs(session, run_index)
            counts["campaigns"] = _reconcile_campaigns(session, campaigns_map)
            counts["evidence_bundles"] = _reconcile_evidence_bundles(
                session, campaigns_dir
            )
            counts["model_stats_seed_rows"] = _reconcile_model_stats_seed(
                session, model_stats
            )
    except Exception as exc:
        log.warning("reconcile_session_failed", extra={"error": repr(exc)})
        return {"error": repr(exc), **counts}

    duration_seconds = time.monotonic() - started
    duration_ms = int(duration_seconds * 1000)
    log.info(
        "reconcile_completed runs=%d campaigns=%d bundles=%d seed_rows=%d duration_ms=%d",
        counts["runs"],
        counts["campaigns"],
        counts["evidence_bundles"],
        counts["model_stats_seed_rows"],
        duration_ms,
    )
    # Phase 2.1.13 metrics — bounded-cardinality counters for the
    # reconcile sweep. Wrapped so a metrics import failure can't take
    # down the lifespan startup.
    try:
        from core.metrics import (
            observe_postgres_reconcile_duration,
            observe_postgres_reconcile_rows,
        )
        observe_postgres_reconcile_rows("runs", counts["runs"])
        observe_postgres_reconcile_rows("campaigns", counts["campaigns"])
        observe_postgres_reconcile_rows(
            "evidence_bundles", counts["evidence_bundles"]
        )
        observe_postgres_reconcile_rows(
            "model_stats_daily", counts["model_stats_seed_rows"]
        )
        observe_postgres_reconcile_duration(duration_seconds)
    except Exception:
        pass
    return {**counts, "duration_ms": duration_ms}
