"""End-to-end smoke for Phase 1.3 Phase B: query_ollama @task -> state hook -> LLM_CALL_LOG.

Verifies that one positionally-called query_ollama inside a Prefect @flow
produces one LlmCallRecord in the per-run buffer with run_id, model, and
response_tokens (eval_count) all captured. Catches wiring regressions in
the @task decorator, state hook param lookup, or LlmResponse envelope
flow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from prefect import flow

from core.llm_call_log import LLM_CALL_LOG
from llm.ollama import query_ollama


@flow(name="test-llm-flow")
def _tiny_flow(run_id: str):
    return query_ollama("qwen2.5-coder:32b", "ping", "http://fake-ollama/api/generate", run_id)


def test_llm_call_records_appended_after_flow_run():
    LLM_CALL_LOG.drain("smoke")  # clean slate

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"response": "pong", "eval_count": 7}

    with patch("llm.ollama.requests.post", return_value=fake_response):
        result = _tiny_flow("smoke")

    assert result.text == "pong"
    assert result.eval_count == 7

    drained = LLM_CALL_LOG.drain("smoke")
    assert len(drained) == 1, f"expected 1 LlmCallRecord, got {len(drained)}"
    assert drained[0].run_id == "smoke"
    assert drained[0].model == "qwen2.5-coder:32b"
    assert drained[0].response_tokens == 7
