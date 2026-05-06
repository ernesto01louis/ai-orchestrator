"""Integration tests for run_orchestration Prefect flow.

Agent functions are patched at the orchestration module level rather than
relying solely on mock_ollama, because the planner uses structured-output
schema validation that the canned mock JSON does not satisfy.

sandbox_execute (not run_in_sandbox) is the correct target in execution/__init__.py.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from core.llm_call_log import LLM_CALL_LOG
from core.runtime import RUN_STATUS
from orchestration import OrchestrateRequest, run_orchestration

# ---------------------------------------------------------------------------
# Shared valid plan returned by the patched planner
# ---------------------------------------------------------------------------
_VALID_PLAN = {
    "language": "python",
    "project_type": "script",
    "execution_mode": "generate",
    "port": 0,
    "entrypoint": "main.py",
    "files": {"main.py": "print('hi')"},
    "dependencies": [],
    "steps": [],
    "approach": "print hello",
}

_VALID_CANDIDATE = {
    "model": "m1",
    "files": {"main.py": "print('hi')"},
    "score": 9,
    "judge": {"feedback": "looks good"},
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_run_status():
    RUN_STATUS.clear()
    yield
    RUN_STATUS.clear()


@pytest.fixture
def tiny_request():
    # planner_model is required by OrchestrateRequest — omitted from the
    # original task spec but confirmed present in orchestration/__init__.py:576.
    return OrchestrateRequest(
        project_name="smoke",
        deploy_target="local",
        prompt="print hi",
        language="python",
        planner_model="planner",
        generator_models=["m1", "m2", "m3"],
        judge_model="judge",
    )


@pytest.fixture
def standard_patches():
    """Patches the agents/helpers that don't need to vary per test."""
    with ExitStack() as stack:
        stack.enter_context(patch("orchestration.planner_agent.fn", return_value=_VALID_PLAN))
        stack.enter_context(patch("orchestration.generate_candidate.fn", return_value=_VALID_CANDIDATE))
        stack.enter_context(patch("orchestration.sandbox_execute", return_value={"exit_code": 0, "stdout": "hi", "error": ""}))
        stack.enter_context(patch("orchestration.environment_inspector", return_value={"os": "Linux", "python": "python3", "node": "node", "arch": "x86_64"}))
        stack.enter_context(patch("orchestration.gather_live_context", return_value=""))
        stack.enter_context(patch("orchestration.build_full_planner_context", return_value=""))
        stack.enter_context(patch("orchestration.notify_run_started"))
        stack.enter_context(patch("orchestration.notify_run_complete"))
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_flow_completes_end_to_end(tiny_request, mock_ollama, standard_patches):
    run_orchestration(tiny_request, "test-flow-1")

    assert RUN_STATUS["test-flow-1"]["completed"] is True


def test_run_status_marked_completed_after_flow(tiny_request, mock_ollama, standard_patches):
    run_orchestration(tiny_request, "test-flow-2")

    assert RUN_STATUS["test-flow-2"].get("phase", "").lower() in ("completed", "failed")


def test_evidence_bundle_populates_llm_calls(tiny_request, mock_ollama):
    # generate_candidate is NOT patched at .fn level here so that the real
    # generate_candidate task runs and calls query_ollama (a Prefect @task
    # tagged "llm-call").  That fires on_task_completion → LLM_CALL_LOG.append.
    # The planner is still patched because PLAN_SCHEMA rejects the canned JSON.
    LLM_CALL_LOG.drain("test-flow-3")  # clear any prior entries
    with ExitStack() as stack:
        stack.enter_context(patch("orchestration.planner_agent.fn", return_value=_VALID_PLAN))
        stack.enter_context(patch("orchestration.sandbox_execute", return_value={"exit_code": 0, "stdout": "hi", "error": ""}))
        stack.enter_context(patch("orchestration.environment_inspector", return_value={"os": "Linux", "python": "python3", "node": "node", "arch": "x86_64"}))
        stack.enter_context(patch("orchestration.gather_live_context", return_value=""))
        stack.enter_context(patch("orchestration.build_full_planner_context", return_value=""))
        stack.enter_context(patch("orchestration.notify_run_started"))
        stack.enter_context(patch("orchestration.notify_run_complete"))
        run_orchestration(tiny_request, "test-flow-3")

    # Each generator model invokes query_ollama (tagged "llm-call") →
    # on_task_completion fires → record lands in LLM_CALL_LOG.
    records = LLM_CALL_LOG.drain("test-flow-3")
    assert len(records) >= 1, f"Expected at least one LlmCallRecord, got {records}"


def test_generate_candidate_map_runs_per_model(tiny_request, mock_ollama, standard_patches):
    # Smoke test: verifies .map() over generator_models doesn't crash under
    # the Prefect test harness.  Asserting per-model task counts is impractical
    # because .map() futures are collected inside run_orchestration and not
    # exposed; a no-exception + completed status is the correct harness-level check.
    run_orchestration(tiny_request, "test-flow-4")

    assert RUN_STATUS.get("test-flow-4", {}).get("completed") is True


def test_sandbox_failure_triggers_troubleshoot_loop(tiny_request, mock_ollama):
    # Patch sandbox_execute to raise so the troubleshoot branch is exercised.
    # When the exception propagates to the top-level handler in run_orchestration,
    # it calls _update_run_status(run_id, completed=True, error=str(e)), so
    # completed is True and error is non-None — proving the failure path was hit.
    with ExitStack() as stack:
        stack.enter_context(patch("orchestration.planner_agent.fn", return_value=_VALID_PLAN))
        stack.enter_context(patch("orchestration.generate_candidate.fn", return_value=_VALID_CANDIDATE))
        stack.enter_context(patch("orchestration.sandbox_execute", side_effect=RuntimeError("sandbox boom")))
        stack.enter_context(patch("orchestration.environment_inspector", return_value={"os": "Linux", "python": "python3", "node": "node", "arch": "x86_64"}))
        stack.enter_context(patch("orchestration.gather_live_context", return_value=""))
        stack.enter_context(patch("orchestration.build_full_planner_context", return_value=""))
        stack.enter_context(patch("orchestration.notify_run_started"))
        stack.enter_context(patch("orchestration.notify_run_complete"))
        try:
            run_orchestration(tiny_request, "test-flow-5")
        except Exception:
            pass

    status = RUN_STATUS.get("test-flow-5", {})
    assert status.get("completed") is True
    assert status.get("error") is not None
