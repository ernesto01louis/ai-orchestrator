"""Tests for Prometheus metrics endpoint and instrument helpers (Phase 1.8.5)."""
from __future__ import annotations

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

def test_metrics_endpoint_returns_prometheus_format(inprocess_client):
    """GET /metrics → 200 text/plain with all four instrument names present."""
    r = inprocess_client.get("/metrics")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert ct.startswith("text/plain"), f"unexpected content-type: {ct}"
    body = r.text
    assert "orchestrator_runs_total" in body
    assert "orchestrator_agent_task_seconds" in body
    assert "orchestrator_llm_calls_total" in body
    assert "orchestrator_active_runs" in body


def test_metrics_endpoint_in_public_paths():
    """/metrics must be in DEFAULT_PUBLIC_PATHS so bearer-token bypass works."""
    from core.auth import DEFAULT_PUBLIC_PATHS
    assert "/metrics" in DEFAULT_PUBLIC_PATHS


# ---------------------------------------------------------------------------
# Counter / gauge helper tests
# ---------------------------------------------------------------------------

def test_runs_total_started_increments_and_active_runs_gauge(inprocess_client):
    """observe_run_started() increments started counter and active_runs gauge."""
    from core.metrics import ACTIVE_RUNS, RUNS_TOTAL, observe_run_started

    before_started = RUNS_TOTAL.labels(status="started")._value.get()
    before_active = ACTIVE_RUNS._value.get()

    observe_run_started()

    assert RUNS_TOTAL.labels(status="started")._value.get() == before_started + 1
    assert ACTIVE_RUNS._value.get() == before_active + 1

    # Also confirm /metrics reflects the new values
    r = inprocess_client.get("/metrics")
    assert r.status_code == 200
    assert "orchestrator_runs_total" in r.text
    assert "orchestrator_active_runs" in r.text


def test_active_runs_gauge_decrements_on_completion():
    """observe_run_started() + observe_run_succeeded() → net-zero gauge change."""
    from core.metrics import ACTIVE_RUNS, observe_run_started, observe_run_succeeded

    before = ACTIVE_RUNS._value.get()
    observe_run_started()
    observe_run_succeeded()
    assert ACTIVE_RUNS._value.get() == before


def test_agent_task_histogram_observes(inprocess_client):
    """observe_agent_task() increments the histogram sample count."""
    from core.metrics import AGENT_TASK_SECONDS, observe_agent_task

    # Use a unique label combo so prior test pollution is irrelevant
    observe_agent_task("planner", "qwen-hist-test", 0.5)

    # The _metrics dict key includes the label values
    sample = AGENT_TASK_SECONDS.labels(role="planner", model="qwen-hist-test")
    assert sample._sum.get() >= 0.5

    r = inprocess_client.get("/metrics")
    assert 'role="planner"' in r.text


def test_llm_calls_total_outcome_label(inprocess_client):
    """observe_llm_call() increments correct outcome label."""
    from core.metrics import LLM_CALLS_TOTAL, observe_llm_call

    before_success = LLM_CALLS_TOTAL.labels(
        role="judge", model="qwen-outcome-test", outcome="success"
    )._value.get()
    before_failure = LLM_CALLS_TOTAL.labels(
        role="judge", model="qwen-outcome-test", outcome="failure"
    )._value.get()

    observe_llm_call("judge", "qwen-outcome-test", success=True)
    observe_llm_call("judge", "qwen-outcome-test", success=False)

    assert LLM_CALLS_TOTAL.labels(
        role="judge", model="qwen-outcome-test", outcome="success"
    )._value.get() == before_success + 1
    assert LLM_CALLS_TOTAL.labels(
        role="judge", model="qwen-outcome-test", outcome="failure"
    )._value.get() == before_failure + 1

    r = inprocess_client.get("/metrics")
    assert 'outcome="success"' in r.text
    assert 'outcome="failure"' in r.text


# ---------------------------------------------------------------------------
# State-hook routing tests
# ---------------------------------------------------------------------------

def test_state_hook_on_failure_routes_timed_out():
    """on_failure routes state.name='TimedOut' → timed_out counter."""
    from core.metrics import RUNS_TOTAL
    from prefect_io.state_hooks import on_failure

    flow = MagicMock()
    flow_run = MagicMock()
    flow_run.parameters = {"run_id": "test-metrics-timedout"}

    # TimedOut case
    state_to = MagicMock()
    state_to.name = "TimedOut"
    state_to.message = "timed out"

    before_to = RUNS_TOTAL.labels(status="timed_out")._value.get()
    on_failure(flow, flow_run, state_to)
    assert RUNS_TOTAL.labels(status="timed_out")._value.get() == before_to + 1

    # Failed case
    state_failed = MagicMock()
    state_failed.name = "Failed"
    state_failed.message = "something went wrong"

    before_failed = RUNS_TOTAL.labels(status="failed")._value.get()
    on_failure(flow, flow_run, state_failed)
    assert RUNS_TOTAL.labels(status="failed")._value.get() == before_failed + 1


def test_state_hook_on_completion_increments_succeeded():
    """on_completion fires observe_run_succeeded()."""
    from core.metrics import RUNS_TOTAL
    from prefect_io.state_hooks import on_completion

    flow = MagicMock()
    flow_run = MagicMock()
    flow_run.parameters = {"run_id": "test-metrics-completed"}

    state = MagicMock()
    state.name = "Completed"

    before = RUNS_TOTAL.labels(status="succeeded")._value.get()
    on_completion(flow, flow_run, state)
    assert RUNS_TOTAL.labels(status="succeeded")._value.get() == before + 1


def test_state_hook_on_cancelled_increments_aborted():
    """on_cancelled fires observe_run_aborted()."""
    from core.metrics import RUNS_TOTAL
    from prefect_io.state_hooks import on_cancelled

    flow = MagicMock()
    flow.name = "orchestrate"
    flow_run = MagicMock()
    flow_run.parameters = {"run_id": "test-metrics-cancelled"}

    state = MagicMock()
    state.name = "Cancelled"

    before = RUNS_TOTAL.labels(status="aborted")._value.get()
    on_cancelled(flow, flow_run, state)
    assert RUNS_TOTAL.labels(status="aborted")._value.get() == before + 1
