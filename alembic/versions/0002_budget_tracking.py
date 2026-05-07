"""Phase 2.4 budget tracking: campaigns.budget_*, llm_calls.prompt_tokens + cost_usd.

Revision ID: 0002_budget_tracking
Revises: 0001_initial_schema
Create Date: 2026-05-07

Adds the columns needed to compute and accrue per-campaign USD spend
from per-call token counts:

* ``campaigns.budget_total_usd`` — operator-set ceiling, NULL means
  unlimited (still tracks usage but never breaches).
* ``campaigns.budget_used_usd`` — running total of ``cost_usd`` from
  every LlmCall belonging to a Run owned by this campaign.
  Reconcile-on-startup re-aggregates from JSON when the orchestrator
  boots, so a brief Postgres outage doesn't drift the canonical value.
* ``campaigns.budget_state`` — one of {``ok``, ``warning``, ``breach``,
  ``paused``}. Transitions out-of-band (state-hook), enforced by check
  constraint.
* ``campaigns.budget_thresholds_emitted`` — small JSONB list of int
  percentages (50, 80, 100). Once a threshold has fired its
  notification we record it here so re-evaluation doesn't double-emit.
* ``llm_calls.prompt_tokens`` + ``llm_calls.cost_usd`` — populated by
  the on_task_completion state hook from the Ollama envelope's
  ``prompt_eval_count`` / ``eval_count`` × the configured rate.

Forward-only. Defaults make this safe to run against a populated
database — existing Campaign / LlmCall rows get ``0.0`` / ``ok`` /
``[]`` / ``0``.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_budget_tracking"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


_BUDGET_STATES = ("ok", "warning", "breach", "paused")


def _check_in(col: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{col} IN ({quoted})"


def upgrade() -> None:
    # ----- campaigns: budget columns -------------------------------------
    op.add_column(
        "campaigns",
        sa.Column("budget_total_usd", sa.Float(), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "budget_used_usd",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "budget_state",
            sa.Text(),
            nullable=False,
            server_default="ok",
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "budget_thresholds_emitted",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_campaigns_budget_state",
        "campaigns",
        _check_in("budget_state", _BUDGET_STATES),
    )

    # ----- llm_calls: cost columns ---------------------------------------
    op.add_column(
        "llm_calls",
        sa.Column(
            "prompt_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "cost_usd",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_calls", "cost_usd")
    op.drop_column("llm_calls", "prompt_tokens")
    op.drop_constraint("ck_campaigns_budget_state", "campaigns", type_="check")
    op.drop_column("campaigns", "budget_thresholds_emitted")
    op.drop_column("campaigns", "budget_state")
    op.drop_column("campaigns", "budget_used_usd")
    op.drop_column("campaigns", "budget_total_usd")
