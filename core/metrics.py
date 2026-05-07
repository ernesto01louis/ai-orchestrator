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
