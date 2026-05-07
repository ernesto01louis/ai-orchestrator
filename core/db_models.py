"""Phase 2.1 ORM mappings + DAO functions for the durable store.

The five ORM classes mirror the Alembic 0001 schema. Pydantic stays the
wire schema (Campaign, EvidenceBundle, etc.); these ORM classes are
internal-only — the DAO functions take and return plain ``dict[str, Any]``
so callers in 2.1.7+ never need to import SQLAlchemy.

Idempotent DAOs (all built on the postgres ``INSERT ... ON CONFLICT``
primitive):

* :func:`upsert_run` — ON CONFLICT (run_id) DO UPDATE; JSON wins on
  the orchestrator side, so we DO UPDATE on every callsite.
* :func:`upsert_campaign` — same pattern.
* :func:`insert_llm_call` — ON CONFLICT (call_id) DO NOTHING. Calls
  never need to be revised after the fact.
* :func:`insert_evidence_bundle` — ON CONFLICT (bundle_id) DO NOTHING.
* :func:`upsert_model_stats_daily` — atomic accumulator. Uses
  ``EXCLUDED.runs + model_stats_daily.runs`` so concurrent campaign
  completions can't race-and-overwrite each other on the same day.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    """Single declarative base for the durable store. Alembic env.py
    imports ``Base.metadata`` for autogenerate."""


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


class Campaign(Base):
    __tablename__ = "campaigns"

    campaign_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    hypothesis: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    template: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    max_runs: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    parallelism: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    merkle_root: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    merkle_status: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Phase 2.4 budget tracking. ``budget_total_usd`` is operator-set;
    # NULL = no ceiling (still tracks usage). ``budget_used_usd`` is the
    # running total — see core/budget.py and the on_task_completion
    # hook for how it's accrued. ``budget_state`` is constrained by the
    # CHECK in alembic 0002 to {ok, warning, breach, paused}.
    budget_total_usd: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    budget_used_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    budget_state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="ok")
    budget_thresholds_emitted: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=list,
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    campaign_id: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("campaigns.campaign_id", ondelete="SET NULL"), nullable=True
    )
    project: Mapped[str] = mapped_column(sa.Text, nullable=False)
    target: Mapped[str] = mapped_column(sa.Text, nullable=False)
    phase: Mapped[str] = mapped_column(sa.Text, nullable=False)
    score: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    completed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    has_error: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    error_msg: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    manifest_status: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------


class LlmCall(Base):
    __tablename__ = "llm_calls"

    call_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    agent_role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_digest: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    model_size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    host: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    response_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # Phase 2.4 budget. ``prompt_tokens`` is the input side
    # (Ollama's ``prompt_eval_count``); ``cost_usd`` is the rate-table
    # × token-counts product computed by core/budget.cost_usd_for in
    # the on_task_completion state hook before the row is upserted.
    prompt_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    sampling: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    rendered_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    response_text: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")


# ---------------------------------------------------------------------------
# Evidence bundles
# ---------------------------------------------------------------------------


class EvidenceBundle(Base):
    __tablename__ = "evidence_bundles"

    bundle_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        sa.Text, sa.ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(sa.Text, nullable=False, default="1.0.0")
    crate_path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    crate_sha256: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    signed_by_keyid: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


# ---------------------------------------------------------------------------
# Model stats daily
# ---------------------------------------------------------------------------


class ModelStatsDaily(Base):
    __tablename__ = "model_stats_daily"

    model_name: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    date: Mapped[Any] = mapped_column(sa.Date, primary_key=True)
    runs: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    total_score: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    wins: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    by_language: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    by_role: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    by_project_type: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(sa.Text, nullable=False, default="live")
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


# ===========================================================================
# DAOs
# ===========================================================================
#
# Each DAO takes a Session and a plain dict (no Pydantic dependency on
# the ORM side; conversion happens at the writethrough wrapper in 2.1.7).
# Returns nothing — callers never need to inspect the row after writing.

# Columns that callers may pass through to upserts. Keeping these as
# tuples (not the ORM column lookups) makes the DAOs robust to ORM
# refactors and keeps test mocks dead simple.
_RUN_UPSERT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "campaign_id",
    "project",
    "target",
    "phase",
    "score",
    "completed",
    "has_error",
    "error_msg",
    "manifest_sha256",
    "manifest_status",
    "started_at",
    "completed_at",
    "params",
)

_CAMPAIGN_UPSERT_COLUMNS: tuple[str, ...] = (
    "campaign_id",
    "name",
    "description",
    "hypothesis",
    "status",
    "template",
    "params",
    "max_runs",
    "parallelism",
    "created_at",
    "started_at",
    "completed_at",
    "merkle_root",
    "merkle_status",
    # Phase 2.4 budget columns (alembic 0002_budget_tracking).
    "budget_total_usd",
    "budget_used_usd",
    "budget_state",
    "budget_thresholds_emitted",
)


def _project_dict(data: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    """Drop unknown keys and skip ``None`` for non-PK fields so DEFAULTs apply."""
    return {k: data[k] for k in allowed if k in data}


def upsert_run(session: Session, data: dict[str, Any]) -> None:
    """Insert or update a run row (run_id is the conflict key)."""
    row = _project_dict(data, _RUN_UPSERT_COLUMNS)
    if "run_id" not in row:
        raise ValueError("upsert_run requires 'run_id'")
    if "completed_at" not in row:
        # NOT NULL on the table — caller must set it.
        raise ValueError("upsert_run requires 'completed_at'")
    stmt = pg_insert(Run).values(**row)
    update_cols = {k: stmt.excluded[k] for k in row if k != "run_id"}
    if update_cols:
        stmt = stmt.on_conflict_do_update(
            index_elements=[Run.run_id],
            set_=update_cols,
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Run.run_id])
    session.execute(stmt)


def upsert_campaign(session: Session, data: dict[str, Any]) -> None:
    """Insert or update a campaign row (campaign_id is the conflict key)."""
    row = _project_dict(data, _CAMPAIGN_UPSERT_COLUMNS)
    if "campaign_id" not in row:
        raise ValueError("upsert_campaign requires 'campaign_id'")
    if "name" not in row or "status" not in row or "created_at" not in row:
        raise ValueError("upsert_campaign requires 'name', 'status', 'created_at'")
    stmt = pg_insert(Campaign).values(**row)
    update_cols = {k: stmt.excluded[k] for k in row if k != "campaign_id"}
    if update_cols:
        stmt = stmt.on_conflict_do_update(
            index_elements=[Campaign.campaign_id],
            set_=update_cols,
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Campaign.campaign_id])
    session.execute(stmt)


def insert_llm_call(session: Session, data: dict[str, Any]) -> None:
    """Insert one llm_calls row, idempotently. Re-runs (e.g. retries) of
    the same Prefect task share a call_id — DO NOTHING on conflict."""
    if "call_id" not in data or "run_id" not in data:
        raise ValueError("insert_llm_call requires 'call_id' and 'run_id'")
    stmt = pg_insert(LlmCall).values(**data).on_conflict_do_nothing(
        index_elements=[LlmCall.call_id]
    )
    session.execute(stmt)


def insert_evidence_bundle(session: Session, data: dict[str, Any]) -> None:
    """Insert an evidence_bundles row referencing the on-disk RO-Crate.
    Re-running build_bundle for the same campaign creates a new
    bundle_id (ULID) — so we DO NOTHING on conflict, but in practice
    every bundle is unique."""
    required = {"bundle_id", "campaign_id", "crate_path", "crate_sha256", "created_at"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"insert_evidence_bundle missing fields: {missing}")
    stmt = pg_insert(EvidenceBundle).values(**data).on_conflict_do_nothing(
        index_elements=[EvidenceBundle.bundle_id]
    )
    session.execute(stmt)


def upsert_model_stats_daily(session: Session, delta: dict[str, Any]) -> None:
    """Atomic accumulator for the (model_name, date) row.

    Adds the delta's counters to whatever's already in the row, replaces
    the jsonb breakdowns with the merged form passed in (callers
    pre-merge so we keep the SQL simple), and bumps updated_at.

    Required keys: ``model_name``, ``date``, ``updated_at``. Counter
    fields default to 0 when omitted.
    """
    if "model_name" not in delta or "date" not in delta:
        raise ValueError("upsert_model_stats_daily requires 'model_name' and 'date'")
    if "updated_at" not in delta:
        raise ValueError("upsert_model_stats_daily requires 'updated_at'")

    payload = {
        "model_name": delta["model_name"],
        "date": delta["date"],
        "runs": int(delta.get("runs", 0)),
        "total_score": float(delta.get("total_score", 0.0)),
        "wins": int(delta.get("wins", 0)),
        "failures": int(delta.get("failures", 0)),
        "by_language": delta.get("by_language", {}) or {},
        "by_role": delta.get("by_role", {}) or {},
        "by_project_type": delta.get("by_project_type", {}) or {},
        "source": delta.get("source", "live"),
        "updated_at": delta["updated_at"],
    }
    stmt = pg_insert(ModelStatsDaily).values(**payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ModelStatsDaily.model_name, ModelStatsDaily.date],
        set_={
            "runs": ModelStatsDaily.runs + stmt.excluded.runs,
            "total_score": ModelStatsDaily.total_score + stmt.excluded.total_score,
            "wins": ModelStatsDaily.wins + stmt.excluded.wins,
            "failures": ModelStatsDaily.failures + stmt.excluded.failures,
            "by_language": stmt.excluded.by_language,
            "by_role": stmt.excluded.by_role,
            "by_project_type": stmt.excluded.by_project_type,
            "updated_at": stmt.excluded.updated_at,
            # 'source' intentionally NOT updated on conflict — once a row
            # is 'live', we never demote it to 'reconcile_seed'.
        },
    )
    session.execute(stmt)


__all__ = [
    "Base",
    "Campaign",
    "EvidenceBundle",
    "LlmCall",
    "ModelStatsDaily",
    "Run",
    "insert_evidence_bundle",
    "insert_llm_call",
    "upsert_campaign",
    "upsert_model_stats_daily",
    "upsert_run",
]
