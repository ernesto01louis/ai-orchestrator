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


# Phase 2 hardening: Redis pub/sub subscriber heartbeat + watchdog.
#
# The WS-broadcast subscriber polls ``pubsub.get_message(timeout=1.0)``
# in a daemon thread. Pre-Phase-2 the thread silently exited on
# connection death — the ``_ws_subscriber_died`` log line was the only
# signal, easy to miss in journalctl. These counters give Grafana a
# reliable "is the subscriber alive?" probe.
#
# ``outcome`` for HEARTBEAT_TOTAL is one of {"tick", "error"}; the
# tick rate is roughly 1Hz under idle traffic (matches the get_message
# timeout). Alert when ticks fall to zero for >30s.
#
# RESTARTS_TOTAL increments every time the supervisor re-subscribes
# after a connection-death exception in the get_message loop. A
# non-zero rate flags persistent Redis issues even when the
# subscriber is technically "alive".

REDIS_SUBSCRIBER_HEARTBEAT_TOTAL = Counter(
    "orchestrator_redis_subscriber_heartbeat_total",
    "Redis WS-broadcast subscriber polls by outcome.",
    ["outcome"],  # tick | error
)

REDIS_SUBSCRIBER_RESTARTS_TOTAL = Counter(
    "orchestrator_redis_subscriber_restarts_total",
    "Times the Redis WS-broadcast subscriber re-subscribed after a "
    "connection-death exception in the poll loop.",
)


def observe_redis_subscriber_tick() -> None:
    REDIS_SUBSCRIBER_HEARTBEAT_TOTAL.labels(outcome="tick").inc()


def observe_redis_subscriber_error() -> None:
    REDIS_SUBSCRIBER_HEARTBEAT_TOTAL.labels(outcome="error").inc()


def observe_redis_subscriber_restart() -> None:
    REDIS_SUBSCRIBER_RESTARTS_TOTAL.inc()


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


# ---------------------------------------------------------------------------
# Phase 3.3 NoteDiscovery metrics
# ---------------------------------------------------------------------------
# ``outcome`` is one of {success, empty, failure, disabled}. Bounded
# cardinality (4 values).

NOTEDISCOVERY_QUERIES_TOTAL = Counter(
    "orchestrator_notediscovery_queries_total",
    "NoteDiscovery search outcomes from the planner research step.",
    ["outcome"],
)

NOTEDISCOVERY_QUERY_DURATION = Histogram(
    "orchestrator_notediscovery_query_duration_seconds",
    "NoteDiscovery search call duration (success or failure).",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)


def observe_note_discovery_query(outcome: str, duration_seconds: float) -> None:
    NOTEDISCOVERY_QUERIES_TOTAL.labels(outcome=outcome or "unknown").inc()
    NOTEDISCOVERY_QUERY_DURATION.observe(max(0.0, float(duration_seconds)))


# ---------------------------------------------------------------------------
# Repo-screening spike (2026-05-11) — chonkie chunking metrics
# ---------------------------------------------------------------------------
# ``site`` is one of the small set of call origins (today expected:
# {"references", "vault", "measurement", "unspecified"}); ``chunker``
# is the chonkie variant ({"recursive"} today). Bounded cardinality.
# No ``run_id`` / ``campaign_id`` labels.

CHUNKING_CHUNKS_TOTAL = Counter(
    "orchestrator_chunking_chunks_total",
    "Total chunks emitted by core.chunking.chunk_text, by site and chunker.",
    ["site", "chunker"],
)


def observe_chunking(*, site: str, chunker: str, count: int) -> None:
    if count <= 0:
        return
    CHUNKING_CHUNKS_TOTAL.labels(
        site=site or "unknown",
        chunker=chunker or "unknown",
    ).inc(count)


# ---------------------------------------------------------------------------
# Repo-screening spike (2026-05-11) — deepeval scoring metrics
# ---------------------------------------------------------------------------
# Cardinality bounded:
# - ``metric`` is one of the small handful of G-Eval rubrics in use
#   (typically named per suite; today expected: {"g_eval", "correctness",
#   "instruction_following"}).
# - ``judge_model`` is the Ollama model name (one of a few; the orchestrator
#   doesn't dynamically generate model names).
# - ``outcome`` for the counter is one of {passed, failed, disabled,
#   empty_input, error}.
# No run_id / campaign_id labels.

EVAL_SCORE = Histogram(
    "orchestrator_eval_score",
    "G-Eval scores returned by the deepeval primitive, by metric and judge.",
    ["metric", "judge_model"],
    buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

EVAL_OUTCOMES_TOTAL = Counter(
    "orchestrator_eval_outcomes_total",
    "G-Eval call outcomes by metric, judge, and pass/fail/error bucket.",
    ["metric", "judge_model", "outcome"],
)


def observe_eval_score(
    *, metric: str, judge_model: str, score: float, outcome: str,
) -> None:
    """Record one G-Eval call. Score histogram is observed only for
    real judge calls (outcome in {passed, failed}); skip / error
    outcomes still bump the counter so harness operators see them."""
    safe_metric = metric or "unknown"
    safe_judge = judge_model or "unknown"
    safe_outcome = outcome or "unknown"
    EVAL_OUTCOMES_TOTAL.labels(
        metric=safe_metric, judge_model=safe_judge, outcome=safe_outcome,
    ).inc()
    if safe_outcome in {"passed", "failed"}:
        clamped = max(0.0, min(1.0, float(score)))
        EVAL_SCORE.labels(metric=safe_metric, judge_model=safe_judge).observe(clamped)
