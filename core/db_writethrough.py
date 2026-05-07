"""Phase 2.1 Postgres write-through chokepoint.

Every dual-write callsite goes through one of these ``mirror_*``
functions instead of touching ``core.db`` directly. Centralising the
policy here means the failure semantics and observability are
consistent across every JSON-canonical write in the codebase.

Policy (per the Phase 2.1 plan):

1. **JSON first, Postgres second.** Callers must have already written
   the JSON file by the time they call us. We never raise.
2. **No-op when disabled.** ``core.db.is_enabled()`` is the gate. With
   ``postgres.enabled=false`` (the default), every mirror_* function
   returns immediately, no DB connection attempted.
3. **Swallow on failure.** Postgres unreachable / SQL error / timeout
   → log a structured WARN and return. JSON is canonical, reconcile
   on next startup heals the gap. We never propagate exceptions out
   of a Prefect ``@task`` body via this path (matches obs 180).
4. **Statement timeout.** Inherited from ``core.db.get_session``,
   bounded to ``postgres.statement_timeout_ms`` so a sluggish DB can't
   stall callers.

Phase 2.1.13 hooks Prometheus counters to the WARN path so dashboards
can see writethrough failures without grepping logs.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core import db, db_models

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversion helpers (JSON shape → DAO input shape)
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-format timestamp tolerantly. Returns None on falsy
    input. Strings without a timezone are interpreted as UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _run_snapshot_to_row(run_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Convert a RUN_STATUS snapshot (passed to _persist_run_index) into
    upsert_run input. Only fields present in the snapshot are forwarded;
    ``manifest_sha256`` is always NULL on dual-write — reconcile fills
    it later from the on-disk manifest.json.
    """
    err = snapshot.get("error")
    return {
        "run_id": run_id,
        "project": snapshot.get("project", "") or "",
        "target": snapshot.get("target", "") or "",
        "phase": snapshot.get("phase", "completed") or "completed",
        "score": float(snapshot.get("score") or 0),
        "completed": True,
        "has_error": err is not None,
        "error_msg": (str(err)[:200] if err else None),
        "manifest_status": snapshot.get("manifest_status"),
        "completed_at": _utcnow(),
    }


def _campaign_record_to_row(
    campaign_id: str, record: dict[str, Any]
) -> dict[str, Any]:
    """Convert a campaigns.json record into upsert_campaign input.

    Authoritative ``campaign_id`` is the dict key, not the record's
    ``id`` field — they normally agree, but the key wins.
    """
    return {
        "campaign_id": campaign_id,
        "name": record.get("name", "") or "",
        "description": record.get("description"),
        "hypothesis": record.get("hypothesis", "") or "",
        "status": record.get("status", "queued") or "queued",
        "template": record.get("template") or {},
        "params": record.get("params") or {},
        "max_runs": record.get("max_runs"),
        "parallelism": int(record.get("parallelism", 1) or 1),
        "created_at": _parse_iso(record.get("created_at")) or _utcnow(),
        "started_at": _parse_iso(record.get("started_at")),
        "completed_at": _parse_iso(record.get("completed_at")),
        # merkle_root / merkle_status fill via reconcile from the
        # on-disk merkle.json — campaigns.json never carries them.
        "merkle_root": None,
        "merkle_status": None,
        # Phase 2.4 budget columns. JSON is canonical, Postgres mirrors;
        # ``budget_used_usd`` accrues in core.budget.accrue_to_campaign.
        "budget_total_usd": _coerce_float_or_none(record.get("budget_total_usd")),
        "budget_used_usd": float(record.get("budget_used_usd", 0.0) or 0.0),
        "budget_state": str(record.get("budget_state", "ok") or "ok"),
        "budget_thresholds_emitted": list(
            record.get("budget_thresholds_emitted", []) or []
        ),
    }


def _coerce_float_or_none(v: Any) -> float | None:
    """Helper for optional float fields — preserves NULL for empty input."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public mirror_* entry points
# ---------------------------------------------------------------------------


def _observe(table: str, success: bool) -> None:
    """Increment the dual-write counter without coupling to metrics."""
    try:
        from core.metrics import observe_postgres_writethrough
        observe_postgres_writethrough(table, success=success)
    except Exception:
        # Never let observability fail the writethrough path.
        pass


def mirror_run_completion(run_id: str, snapshot: dict[str, Any]) -> None:
    """Mirror a completed run into the runs table.

    Called from ``core.runtime._persist_run_index`` AFTER the JSON
    file write succeeds. Failures here log + return.
    """
    if not db.is_enabled():
        return
    try:
        row = _run_snapshot_to_row(run_id, snapshot)
        with db.get_session() as session:
            db_models.upsert_run(session, row)
        _observe("runs", success=True)
    except Exception as exc:
        _observe("runs", success=False)
        log.warning(
            "postgres_writethrough_failed",
            extra={
                "table": "runs",
                "run_id": run_id,
                "error": repr(exc),
            },
        )


def mirror_campaigns(
    campaigns_map: dict[str, dict[str, Any]],
    changed_ids: set[str] | None = None,
) -> None:
    """Mirror selected campaigns into the campaigns table.

    Called from ``memory_pkg.save_campaigns`` AFTER the JSON file write
    succeeds. ``changed_ids`` scopes the upserts to the campaigns the
    caller actually modified — passing ``None`` means "upsert every
    campaign in the map", which is what the reconciler uses for a
    full sweep but is wasteful on the hot path.
    """
    if not db.is_enabled():
        return
    keys: set[str] = (
        set(campaigns_map.keys()) if changed_ids is None else set(changed_ids)
    )
    if not keys:
        return
    try:
        with db.get_session() as session:
            for cid in keys:
                record = campaigns_map.get(cid)
                if record is None:
                    continue
                row = _campaign_record_to_row(cid, record)
                db_models.upsert_campaign(session, row)
        _observe("campaigns", success=True)
    except Exception as exc:
        _observe("campaigns", success=False)
        log.warning(
            "postgres_writethrough_failed",
            extra={
                "table": "campaigns",
                "changed_ids": sorted(keys),
                "error": repr(exc),
            },
        )


def _llm_call_record_to_row(record: Any) -> dict[str, Any]:
    """Convert an LlmCallRecord (dataclass from core.llm_call_log) into
    insert_llm_call input. Field names rename from Pydantic to ORM:
    ``model`` → ``model_name``, ``server_url`` → ``host``.
    """
    return {
        "call_id": getattr(record, "call_id", "") or "",
        "run_id": record.run_id,
        "agent_role": getattr(record, "agent_role", "") or "",
        "model_name": record.model,
        "model_digest": getattr(record, "model_digest", "") or None,
        "model_size_bytes": int(getattr(record, "model_size_bytes", 0) or 0),
        "host": getattr(record, "server_url", "") or "",
        "started_at": getattr(record, "started_at", None),
        "duration_ms": int(getattr(record, "duration_ms", 0) or 0),
        "response_tokens": int(getattr(record, "response_tokens", 0) or 0),
        # Phase 2.4 budget columns. Defaults keep legacy LlmCallRecord
        # constructors working (zero cost — same as a missing rate
        # entry, so the row is faithful).
        "prompt_tokens": int(getattr(record, "prompt_tokens", 0) or 0),
        "cost_usd": float(getattr(record, "cost_usd", 0.0) or 0.0),
        "sampling": getattr(record, "sampling", {}) or {},
        "rendered_messages": getattr(record, "rendered_messages", []) or [],
        "response_text": getattr(record, "response_text", "") or "",
    }


def mirror_evidence_bundle(
    bundle: Any,
    *,
    crate_path: str,
    crate_sha256: str,
    signed_by_keyid: str | None = None,
) -> None:
    """Mirror an evidence-bundle into the evidence_bundles table.

    Called from ``evidence.builder.build_bundle`` AFTER the RO-Crate
    is written to disk and signed. We index the on-disk crate (path +
    DSSE-envelope sha256) — never duplicate the JSON-LD payload into
    Postgres.
    """
    if not db.is_enabled():
        return
    try:
        row = {
            "bundle_id": bundle.bundle_id,
            "campaign_id": bundle.campaign_id,
            "schema_version": getattr(bundle, "schema_version", "1.0.0"),
            "crate_path": crate_path,
            "crate_sha256": crate_sha256,
            "created_at": bundle.created_at,
            "signed_by_keyid": signed_by_keyid,
        }
        with db.get_session() as session:
            db_models.insert_evidence_bundle(session, row)
        _observe("evidence_bundles", success=True)
    except Exception as exc:
        _observe("evidence_bundles", success=False)
        log.warning(
            "postgres_writethrough_failed",
            extra={
                "table": "evidence_bundles",
                "bundle_id": getattr(bundle, "bundle_id", ""),
                "campaign_id": getattr(bundle, "campaign_id", ""),
                "error": repr(exc),
            },
        )


def mirror_llm_call(record: Any) -> None:
    """Eagerly mirror one LlmCallRecord into the llm_calls table.

    Called from ``prefect_io.state_hooks.on_task_completion`` immediately
    after the record is appended to ``LLM_CALL_LOG``. Eager (per-call)
    insert, not drain-and-bulk at bundle build, so Phase 2.4 budget
    dashboards see live mid-campaign cost.

    Skips silently when ``call_id`` is empty — legacy code paths that
    don't pass a Prefect task-run ID would all collide on the empty PK
    and get dropped by ON CONFLICT DO NOTHING anyway. The JSON path
    (LLM_CALL_LOG → evidence bundle) still captures the call.
    """
    if not db.is_enabled():
        return
    if not getattr(record, "call_id", ""):
        # Empty call_id — skip Postgres mirror (see docstring).
        return
    try:
        row = _llm_call_record_to_row(record)
        with db.get_session() as session:
            db_models.insert_llm_call(session, row)
        _observe("llm_calls", success=True)
    except Exception as exc:
        _observe("llm_calls", success=False)
        log.warning(
            "postgres_writethrough_failed",
            extra={
                "table": "llm_calls",
                "call_id": getattr(record, "call_id", ""),
                "run_id": getattr(record, "run_id", ""),
                "error": repr(exc),
            },
        )


def mirror_model_stats_daily(
    *,
    model: str,
    score: float,
    was_winner: bool,
    succeeded: bool,
    by_language: dict[str, Any] | None = None,
    by_role: dict[str, Any] | None = None,
    by_project_type: dict[str, Any] | None = None,
) -> None:
    """Mirror a single model-stats update into today's row.

    Called from ``memory_pkg.update_model_stats`` after the JSON file
    write succeeds. Counter columns (``runs``, ``total_score``,
    ``wins``, ``failures``) are atomically incremented server-side via
    the DAO's ``ON CONFLICT … DO UPDATE SET runs = … + EXCLUDED.runs``
    pattern — concurrent campaigns can't race-and-overwrite each other
    on the counters.

    The ``by_*`` jsonb columns are best-effort point-in-time snapshots
    of the JSON-canonical aggregate; on conflict the DAO REPLACES them
    rather than deep-merging (jsonb has no native deep-merge before
    PG14, and the JSON file is canonical anyway). Reconcile-on-startup
    re-syncs them.
    """
    if not db.is_enabled():
        return
    try:
        delta = {
            "model_name": model,
            "date": datetime.now(UTC).date(),
            "runs": 1,
            "total_score": float(score),
            "wins": 1 if was_winner else 0,
            "failures": 0 if succeeded else 1,
            "by_language": by_language or {},
            "by_role": by_role or {},
            "by_project_type": by_project_type or {},
            "source": "live",
            "updated_at": _utcnow(),
        }
        with db.get_session() as session:
            db_models.upsert_model_stats_daily(session, delta)
        _observe("model_stats_daily", success=True)
    except Exception as exc:
        _observe("model_stats_daily", success=False)
        log.warning(
            "postgres_writethrough_failed",
            extra={
                "table": "model_stats_daily",
                "model": model,
                "error": repr(exc),
            },
        )


__all__ = [
    "mirror_campaigns",
    "mirror_evidence_bundle",
    "mirror_llm_call",
    "mirror_model_stats_daily",
    "mirror_run_completion",
]
