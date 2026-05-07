"""Phase 2.1 initial schema: campaigns, runs, llm_calls, evidence_bundles, model_stats_daily.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-07

Mirrors the JSON shapes the orchestrator already writes under
``memory/`` / ``runs/`` / ``campaigns/``. JSON files stay canonical;
these tables are the queryable mirror that Phase 2.4 budget tracking
and Phase 2.6 UI list/filter/sort will read from.

Design notes:
* All primary keys are TEXT to match the UUID-shaped IDs already in use
  in JSON (``run_id``, ``campaign_id``, etc).
* Timestamps are ``TIMESTAMP WITH TIME ZONE``.
* ``runs.campaign_id`` is FK ON DELETE SET NULL so ad-hoc / un-attached
  runs can survive their parent campaign being purged. ``llm_calls``
  cascades because LLM-call rows are meaningless without their parent
  run row.
* ``manifest_status`` / ``merkle_status`` columns capture Phase 1.5
  integrity attestation in the schema without giving manifests their
  own table.
* ``model_stats_daily`` uses a composite (model_name, date) PK so
  ``INSERT ... ON CONFLICT ... DO UPDATE SET runs = ... + EXCLUDED.runs``
  serves as the atomic increment. The ``source`` column distinguishes
  the one-time reconcile-seed row from live rows so Grafana queries can
  exclude the synthetic seed from trend charts.
* GIN indexes on the jsonb breakdown columns let Phase 2.4 budget
  queries filter by language / role / project_type without scanning
  every row.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# CHECK constraint values — kept as module constants so 2.1.6 ORM and
# tests can reference the same set without drift.
# ---------------------------------------------------------------------------

CAMPAIGN_STATUSES = (
    "queued",
    "running",
    "paused",
    "completed",
    "aborted",
    "failed",
)

INTEGRITY_STATUSES = (
    "ok",
    "corrupted",
    "missing",
    "skipped",
)

MODEL_STATS_SOURCES = ("live", "reconcile_seed")


def _check_in(column: str, allowed: tuple[str, ...]) -> str:
    """Render a ``column IN (...)`` SQL fragment for a CHECK constraint."""
    quoted = ", ".join(f"'{v}'" for v in allowed)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    # ----- campaigns ------------------------------------------------------
    op.create_table(
        "campaigns",
        sa.Column("campaign_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("hypothesis", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("template", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("parallelism", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merkle_root", sa.Text(), nullable=True),
        sa.Column("merkle_status", sa.Text(), nullable=True),
        sa.CheckConstraint(_check_in("status", CAMPAIGN_STATUSES), name="ck_campaigns_status"),
        sa.CheckConstraint(
            f"merkle_status IS NULL OR {_check_in('merkle_status', INTEGRITY_STATUSES)}",
            name="ck_campaigns_merkle_status",
        ),
    )
    op.create_index("ix_campaigns_status", "campaigns", ["status"])
    op.create_index(
        "ix_campaigns_created_at_desc",
        "campaigns",
        [sa.text("created_at DESC")],
    )

    # ----- runs -----------------------------------------------------------
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Text(),
            sa.ForeignKey("campaigns.campaign_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("has_error", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("manifest_sha256", sa.Text(), nullable=True),
        sa.Column("manifest_status", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            f"manifest_status IS NULL OR {_check_in('manifest_status', INTEGRITY_STATUSES)}",
            name="ck_runs_manifest_status",
        ),
    )
    op.create_index("ix_runs_campaign_id", "runs", ["campaign_id"])
    op.create_index(
        "ix_runs_completed_at_desc",
        "runs",
        [sa.text("completed_at DESC")],
    )
    op.create_index(
        "ix_runs_project_completed_at_desc",
        "runs",
        ["project", sa.text("completed_at DESC")],
    )

    # ----- llm_calls ------------------------------------------------------
    op.create_table(
        "llm_calls",
        sa.Column("call_id", sa.Text(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Text(),
            sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_role", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_digest", sa.Text(), nullable=True),
        sa.Column("model_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("host", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "sampling",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "rendered_messages",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("response_text", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_llm_calls_run_id", "llm_calls", ["run_id"])
    op.create_index(
        "ix_llm_calls_model_started_at",
        "llm_calls",
        ["model_name", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_llm_calls_started_at_desc",
        "llm_calls",
        [sa.text("started_at DESC")],
    )

    # ----- evidence_bundles ----------------------------------------------
    op.create_table(
        "evidence_bundles",
        sa.Column("bundle_id", sa.Text(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Text(),
            sa.ForeignKey("campaigns.campaign_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Text(), nullable=False, server_default="1.0.0"),
        sa.Column("crate_path", sa.Text(), nullable=False),
        sa.Column("crate_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signed_by_keyid", sa.Text(), nullable=True),
        sa.UniqueConstraint("campaign_id", "bundle_id", name="uq_evidence_bundles_campaign_bundle"),
    )
    op.create_index(
        "ix_evidence_bundles_campaign_created_at_desc",
        "evidence_bundles",
        ["campaign_id", sa.text("created_at DESC")],
    )

    # ----- model_stats_daily ---------------------------------------------
    op.create_table(
        "model_stats_daily",
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "by_language",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "by_role",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "by_project_type",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source", sa.Text(), nullable=False, server_default="live"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("model_name", "date", name="pk_model_stats_daily"),
        sa.CheckConstraint(_check_in("source", MODEL_STATS_SOURCES), name="ck_model_stats_daily_source"),
    )
    op.create_index(
        "ix_model_stats_daily_by_language",
        "model_stats_daily",
        ["by_language"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_model_stats_daily_by_role",
        "model_stats_daily",
        ["by_role"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_model_stats_daily_by_project_type",
        "model_stats_daily",
        ["by_project_type"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_table("model_stats_daily")
    op.drop_table("evidence_bundles")
    op.drop_table("llm_calls")
    op.drop_table("runs")
    op.drop_table("campaigns")
