"""Prometheus metrics — orchestrator-wide.

Defines the four metric instruments and small ``observe_*`` helpers so call
sites don't import prometheus_client directly. The default registry is
used (matches ``prometheus_client.generate_latest()`` at /metrics).

Cardinality discipline:
- run_id is NOT a label (unbounded). Run-id slicing is done in Grafana
  via log/trace correlation, not via Prom labels.
- role/model labels are bounded by agents/* roles and the small set of
  Ollama models loaded on the LAN.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

RUNS_TOTAL = Counter(
    "orchestrator_runs_total",
    "Total orchestrator runs by terminal status.",
    ["status"],  # one of: started, succeeded, failed, aborted, timed_out
)

AGENT_TASK_SECONDS = Histogram(
    "orchestrator_agent_task_seconds",
    "Wall-clock duration of @task agent runs (seconds).",
    ["role", "model"],
)

LLM_CALLS_TOTAL = Counter(
    "orchestrator_llm_calls_total",
    "Total LLM call observations by (role, model, outcome).",
    ["role", "model", "outcome"],  # outcome in {"success", "failure"}
)

ACTIVE_RUNS = Gauge(
    "orchestrator_active_runs",
    "Number of orchestrator runs currently in flight.",
)


# ── small helpers (so call sites don't import prometheus_client) ──────

def observe_run_started() -> None:
    RUNS_TOTAL.labels(status="started").inc()
    ACTIVE_RUNS.inc()


def observe_run_succeeded() -> None:
    RUNS_TOTAL.labels(status="succeeded").inc()
    ACTIVE_RUNS.dec()


def observe_run_failed() -> None:
    RUNS_TOTAL.labels(status="failed").inc()
    ACTIVE_RUNS.dec()


def observe_run_aborted() -> None:
    RUNS_TOTAL.labels(status="aborted").inc()
    ACTIVE_RUNS.dec()


def observe_run_timed_out() -> None:
    RUNS_TOTAL.labels(status="timed_out").inc()
    ACTIVE_RUNS.dec()


def observe_agent_task(role: str, model: str, duration_seconds: float) -> None:
    AGENT_TASK_SECONDS.labels(
        role=role or "unknown",
        model=model or "unknown",
    ).observe(max(duration_seconds, 0.0))


def observe_llm_call(role: str, model: str, *, success: bool) -> None:
    LLM_CALLS_TOTAL.labels(
        role=role or "unknown",
        model=model or "unknown",
        outcome="success" if success else "failure",
    ).inc()


# ── Phase 2.1 Postgres write-through metrics ────────────────────────────
#
# Visibility for the dual-write path. Cardinality is bounded: ``table`` is
# one of {runs, campaigns, llm_calls, evidence_bundles, model_stats_daily}
# and ``outcome`` is one of {success, failure}. Reconcile metrics use the
# same ``table`` set without an outcome label (we count successful inserts
# only — failures already increment ``orchestrator_postgres_writethrough``
# with table=… outcome=failure).

POSTGRES_WRITETHROUGH_TOTAL = Counter(
    "orchestrator_postgres_writethrough_total",
    "Total dual-write attempts to Postgres mirror tables.",
    ["table", "outcome"],  # outcome in {"success", "failure"}
)

POSTGRES_RECONCILE_ROWS_TOTAL = Counter(
    "orchestrator_postgres_reconcile_rows_total",
    "Rows written to each table during reconcile-on-startup.",
    ["table"],
)

POSTGRES_RECONCILE_DURATION_SECONDS = Histogram(
    "orchestrator_postgres_reconcile_duration_seconds",
    "Wall-clock duration of one reconcile-on-startup pass (seconds).",
)


def observe_postgres_writethrough(table: str, *, success: bool) -> None:
    POSTGRES_WRITETHROUGH_TOTAL.labels(
        table=table or "unknown",
        outcome="success" if success else "failure",
    ).inc()


def observe_postgres_reconcile_rows(table: str, rows: int) -> None:
    if rows <= 0:
        return
    POSTGRES_RECONCILE_ROWS_TOTAL.labels(table=table or "unknown").inc(rows)


def observe_postgres_reconcile_duration(seconds: float) -> None:
    POSTGRES_RECONCILE_DURATION_SECONDS.observe(max(seconds, 0.0))


# ---------------------------------------------------------------------------
# Phase 2.2 Redis mirror metrics
# ---------------------------------------------------------------------------
# Visibility for the Redis write-through path. Cardinality is bounded:
# ``operation`` is one of the small handful of write paths (today:
# {run_status_init, run_status_update, run_status_hydrate}); ``outcome``
# is {"success", "failure"}. No ``run_id`` label — Grafana correlates
# by run_id via logs, not metrics labels (mirrors the Postgres-side
# discipline).

REDIS_RUN_STATUS_WRITES_TOTAL = Counter(
    "orchestrator_redis_run_status_writes_total",
    "Redis RUN_STATUS mirror operations by outcome.",
    ["operation", "outcome"],
)


def observe_redis_run_status_write(operation: str, *, success: bool) -> None:
    REDIS_RUN_STATUS_WRITES_TOTAL.labels(
        operation=operation or "unknown",
        outcome="success" if success else "failure",
    ).inc()


# ---------------------------------------------------------------------------
# Phase 2.4 budget metrics
# ---------------------------------------------------------------------------
# Cardinality bounded — ``threshold`` is one of {50, 80, 100} (the
# default thresholds_pct), ``state`` is in {ok, warning, breach,
# paused}. No campaign_id label (stays low-cardinality; Grafana
# correlates by campaign_id via logs).

BUDGET_THRESHOLD_TOTAL = Counter(
    "orchestrator_budget_threshold_total",
    "Budget threshold crossings by percentage and resulting state.",
    ["threshold", "state"],
)


def observe_budget_threshold(threshold_pct: int, state: str) -> None:
    BUDGET_THRESHOLD_TOTAL.labels(
        threshold=str(threshold_pct), state=state or "unknown",
    ).inc()


# ---------------------------------------------------------------------------
# Phase 3.2 SmartPause metrics
# ---------------------------------------------------------------------------
# ``outcome`` is one of:
#   paused          — confidence < threshold AND hitl_mode != full_auto
#   skipped_full_auto — confidence < threshold but mode = full_auto
#   skipped_above   — confidence >= threshold (the common case)
#   skipped_disabled — smartpause.enabled = false
#   timed_out       — pause_timeout_seconds elapsed without resume
#   resumed         — operator hit /runs/{id}/resume
# Bounded cardinality (~6 distinct values).

SMARTPAUSE_TOTAL = Counter(
    "orchestrator_smartpause_total",
    "SmartPause guard outcomes after planner returns.",
    ["outcome"],
)


def observe_smartpause(outcome: str) -> None:
    SMARTPAUSE_TOTAL.labels(outcome=outcome or "unknown").inc()


# ---------------------------------------------------------------------------
# Phase 3.1 HITL metrics
# ---------------------------------------------------------------------------
# ``mode`` is one of {full_auto, gate_only, checkpoint, step_by_step,
# co_pilot}. ``phase`` is one of {post_planner, post_generator,
# post_judge, post_optimizer, post_llm, pre_llm, gate_denied}.
# ``outcome`` is one of {skipped, paused, approve, reject, edit,
# timed_out}. Bounded cardinality (~ 5 * 7 * 6 = 210 max combinations
# across the lifetime of the process).

HITL_TOTAL = Counter(
    "orchestrator_hitl_total",
    "HITL gate outcomes by mode, phase, and operator action.",
    ["mode", "phase", "outcome"],
)


def observe_hitl(*, mode: str, phase: str, outcome: str) -> None:
    HITL_TOTAL.labels(
        mode=mode or "unknown",
        phase=phase or "unknown",
        outcome=outcome or "unknown",
    ).inc()
