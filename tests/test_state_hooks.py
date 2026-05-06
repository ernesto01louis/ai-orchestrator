"""Tests for prefect_io.state_hooks — RUN_STATUS sync + LlmCall capture."""
from unittest.mock import MagicMock, patch

import pytest

from core.llm_call_log import LLM_CALL_LOG
from core.runtime import RUN_STATUS
from prefect_io.state_hooks import (
    on_cancelled,
    on_completion,
    on_failure,
    on_running,
)


@pytest.fixture(autouse=True)
def clear_state():
    RUN_STATUS.clear()
    yield
    RUN_STATUS.clear()


def _mk_flow_ctx(run_id: str = "r1", flow_name: str = "orchestrate"):
    flow = MagicMock()
    flow.name = flow_name
    flow_run = MagicMock()
    flow_run.parameters = {"run_id": run_id}
    state = MagicMock()
    state.name = "Running"
    return flow, flow_run, state


def _mk_task_ctx(tags: list[str], task_name: str = "query_ollama",
                 result: dict | None = None):
    task = MagicMock()
    task.name = task_name
    task.tags = set(tags)
    task_run = MagicMock()
    task_run.parameters = {
        "model": "qwen2.5-coder:32b",
        "messages": [{"role": "user", "content": "hi"}],
        "options": {"temperature": 0.0},
    }
    task_run.start_time = MagicMock()
    task_run.end_time = MagicMock()
    state = MagicMock()
    state.name = "Completed"
    state.result = MagicMock(return_value=result or {"response": "ok",
                                                     "eval_count": 42})
    return task, task_run, state


def test_on_running_initializes_run_status():
    flow, flow_run, state = _mk_flow_ctx(run_id="r1")
    on_running(flow, flow_run, state)
    assert "r1" in RUN_STATUS
    assert RUN_STATUS["r1"]["phase"] == "Running"


def test_on_completion_marks_completed_in_run_status():
    flow, flow_run, state = _mk_flow_ctx(run_id="r2")
    state.name = "Completed"
    on_completion(flow, flow_run, state)
    assert RUN_STATUS["r2"]["completed"] is True
    assert RUN_STATUS["r2"]["phase"] == "Completed"


def test_on_failure_marks_error_in_run_status():
    flow, flow_run, state = _mk_flow_ctx(run_id="r3")
    state.name = "Failed"
    state.message = "boom"
    on_failure(flow, flow_run, state)
    assert RUN_STATUS["r3"]["completed"] is True
    assert "boom" in (RUN_STATUS["r3"].get("error") or "")


def test_on_cancelled_emits_evidence_for_campaign_flow():
    flow, flow_run, state = _mk_flow_ctx(run_id="ignored",
                                          flow_name="campaign")
    flow_run.parameters = {"campaign_id": "camp-1"}
    with patch("prefect_io.state_hooks._safe_emit_evidence") as emit_mock:
        on_cancelled(flow, flow_run, state)
        emit_mock.assert_called_once_with("camp-1")


def test_on_cancelled_no_evidence_for_orchestrate_flow():
    flow, flow_run, state = _mk_flow_ctx(run_id="r1",
                                          flow_name="orchestrate")
    with patch("prefect_io.state_hooks._safe_emit_evidence") as emit_mock:
        on_cancelled(flow, flow_run, state)
        emit_mock.assert_not_called()


def test_task_completion_with_llm_call_tag_appends_to_buffer():
    from prefect_io.state_hooks import on_task_completion

    LLM_CALL_LOG.drain("r4")  # clear
    task, task_run, state = _mk_task_ctx(tags=["llm-call"])
    task_run.parameters = {
        "model": "qwen2.5",
        "messages": [{"role": "user", "content": "hi"}],
        "options": {"temperature": 0.0},
        "run_id": "r4",
    }
    on_task_completion(task, task_run, state)
    drained = LLM_CALL_LOG.drain("r4")
    assert len(drained) == 1
    assert drained[0].run_id == "r4"
    assert drained[0].model == "qwen2.5"


def test_task_completion_without_llm_call_tag_is_noop():
    from prefect_io.state_hooks import on_task_completion

    LLM_CALL_LOG.drain("r5")
    task, task_run, state = _mk_task_ctx(tags=["agent"])
    task_run.parameters = {"run_id": "r5"}
    on_task_completion(task, task_run, state)
    assert LLM_CALL_LOG.drain("r5") == []


def test_task_completion_captures_citation_grade_fields():
    """Phase J β: hook reads call_id, server_url, response_text, started_at,
    digest+size from envelope, agent_role from params."""
    from datetime import datetime, timezone

    from llm.ollama import LlmResponse
    from prefect_io.state_hooks import on_task_completion

    LLM_CALL_LOG.drain("rB")
    started = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 6, 12, 0, 1, tzinfo=timezone.utc)

    task = MagicMock()
    task.tags = {"llm-call"}
    task_run = MagicMock()
    task_run.id = "task-uuid-xyz"
    task_run.start_time = started
    task_run.end_time = end
    task_run.parameters = {
        "model": "qwen2.5",
        "messages": [{"role": "user", "content": "hi"}],
        "options": {"temperature": 0.0},
        "url": "http://192.168.2.13:11434/api/chat",
        "run_id": "rB",
        "agent_role": "judge",
    }

    fake = LlmResponse(
        text="the answer is 42",
        envelope={
            "eval_count": 5,
            "_orchestrator_digest": "sha256-x",
            "_orchestrator_size_bytes": 1234567,
            "_orchestrator_response_text": "the answer is 42",
        },
    )
    state = MagicMock()
    state.name = "Completed"
    state.result = MagicMock(return_value=fake)

    on_task_completion(task, task_run, state)
    drained = LLM_CALL_LOG.drain("rB")
    assert len(drained) == 1
    out = drained[0]
    assert out.call_id == "task-uuid-xyz"
    assert out.agent_role == "judge"
    assert out.server_url == "http://192.168.2.13:11434/api/chat"
    assert out.response_text == "the answer is 42"
    assert out.started_at == started
    assert out.model_digest == "sha256-x"
    assert out.model_size_bytes == 1234567


def test_state_hooks_swallow_unexpected_exceptions(monkeypatch, caplog):
    """A broken hook must never propagate exceptions back into Prefect."""
    flow, flow_run, state = _mk_flow_ctx(run_id="r6")
    monkeypatch.setattr(
        "prefect_io.state_hooks._update_run_status",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    on_running(flow, flow_run, state)
    assert "boom" in caplog.text
