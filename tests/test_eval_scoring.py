"""Tests for eval_pkg/scoring.py — repo-screening deepeval spike.

Covers the dormant path, the three-condition is_enabled() gate, the
zero-score-on-error shape, and the Prom histogram + counter labels.
The live deepeval/Ollama path is NOT exercised here — it takes ~15s
per call and is covered by the measurement harness instead.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core import config
from eval_pkg import scoring


@pytest.fixture(autouse=True)
def _reset_config() -> Any:
    saved = (
        config.EVAL_ENABLED,
        config.EVAL_JUDGE_MODEL,
        config.EVAL_JUDGE_BASE_URL,
        config.EVAL_THRESHOLD,
        config.EVAL_CASE_TIMEOUT_SECONDS,
    )
    yield
    (
        config.EVAL_ENABLED,
        config.EVAL_JUDGE_MODEL,
        config.EVAL_JUDGE_BASE_URL,
        config.EVAL_THRESHOLD,
        config.EVAL_CASE_TIMEOUT_SECONDS,
    ) = saved


# ---------------------------------------------------------------------------
# Dormant + gate behaviour
# ---------------------------------------------------------------------------

def test_dormant_returns_zero_score_with_error_flag() -> None:
    config.EVAL_ENABLED = False
    result = scoring.score_response(
        "What is 2+2?", "The answer is 4.",
        criteria="Does it answer correctly?",
    )
    assert result.score == 0.0
    assert result.passed is False
    assert result.error is True
    assert "disabled" in result.reason.lower()


def test_is_enabled_respects_config_flag() -> None:
    config.EVAL_ENABLED = False
    assert scoring.is_enabled() is False
    config.EVAL_ENABLED = True
    assert scoring.is_enabled() is True


def test_is_enabled_handles_missing_deepeval() -> None:
    """If deepeval (or its ollama optional) is not importable,
    is_enabled returns False even when the config flag is on."""
    config.EVAL_ENABLED = True

    real_import = __import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "deepeval":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_fake_import):
        assert scoring.is_enabled() is False


def test_empty_input_returns_zero_score_with_error_flag() -> None:
    config.EVAL_ENABLED = True
    result = scoring.score_response("", "any output", criteria="any criteria")
    assert result.score == 0.0
    assert result.error is True
    assert "empty" in result.reason.lower()


def test_empty_actual_output_returns_zero_score() -> None:
    config.EVAL_ENABLED = True
    result = scoring.score_response("any input", "", criteria="any criteria")
    assert result.score == 0.0
    assert result.error is True


# ---------------------------------------------------------------------------
# EvalScore shape
# ---------------------------------------------------------------------------

def test_eval_score_to_dict_has_stable_keys() -> None:
    s = scoring.EvalScore(
        metric="m", score=0.42, passed=False, threshold=0.5,
        judge_model="llama3:8b", reason="why",
    )
    d = s.to_dict()
    assert set(d.keys()) == {
        "metric", "score", "passed", "threshold",
        "judge_model", "reason", "error",
    }
    assert d["score"] == 0.42
    assert d["error"] is False  # default


# ---------------------------------------------------------------------------
# Mocked happy path — bypass the real judge
# ---------------------------------------------------------------------------

def test_score_response_uses_mocked_geval_pass() -> None:
    config.EVAL_ENABLED = True

    fake_g_eval = MagicMock()
    fake_g_eval.score = 0.85
    fake_g_eval.reason = "Looks correct."

    with patch.object(scoring, "is_enabled", return_value=True), \
         patch("deepeval.metrics.GEval", return_value=fake_g_eval), \
         patch("deepeval.models.OllamaModel"):
        result = scoring.score_response(
            "What is 2+2?", "4",
            criteria="Does it answer correctly?",
            metric_name="correctness",
        )

    assert result.score == 0.85
    assert result.passed is True
    assert result.metric == "correctness"
    assert result.reason == "Looks correct."
    assert result.error is False
    fake_g_eval.measure.assert_called_once()


def test_score_response_uses_mocked_geval_fail() -> None:
    config.EVAL_ENABLED = True

    fake_g_eval = MagicMock()
    fake_g_eval.score = 0.10
    fake_g_eval.reason = "Wrong answer."

    with patch.object(scoring, "is_enabled", return_value=True), \
         patch("deepeval.metrics.GEval", return_value=fake_g_eval), \
         patch("deepeval.models.OllamaModel"):
        result = scoring.score_response(
            "What is 2+2?", "7",
            criteria="Does it answer correctly?",
        )

    assert result.score == 0.10
    assert result.passed is False
    assert result.error is False


def test_score_response_traps_judge_exception() -> None:
    """Any error from deepeval is trapped and returned as a zero-score
    EvalScore with error=True. Never raises out."""
    config.EVAL_ENABLED = True

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("judge wedged")

    with patch.object(scoring, "is_enabled", return_value=True), \
         patch("deepeval.models.OllamaModel", side_effect=_boom):
        result = scoring.score_response(
            "What is 2+2?", "4",
            criteria="any",
        )

    assert result.score == 0.0
    assert result.passed is False
    assert result.error is True
    assert "judge error" in result.reason.lower()


# ---------------------------------------------------------------------------
# Prom counter
# ---------------------------------------------------------------------------

def test_dormant_bumps_disabled_counter() -> None:
    from core import metrics

    config.EVAL_ENABLED = False
    counter = metrics.EVAL_OUTCOMES_TOTAL.labels(
        metric="g_eval", judge_model="llama3:8b", outcome="disabled",
    )
    before = counter._value.get()
    scoring.score_response("a", "b", criteria="c")
    after = counter._value.get()
    assert after - before == 1


def test_empty_input_bumps_empty_input_counter() -> None:
    from core import metrics

    config.EVAL_ENABLED = True
    counter = metrics.EVAL_OUTCOMES_TOTAL.labels(
        metric="g_eval", judge_model="llama3:8b", outcome="empty_input",
    )
    before = counter._value.get()
    scoring.score_response("", "b", criteria="c")
    after = counter._value.get()
    assert after - before == 1


def test_mocked_pass_bumps_score_histogram_and_passed_counter() -> None:
    from core import metrics

    config.EVAL_ENABLED = True

    fake_g_eval = MagicMock()
    fake_g_eval.score = 0.75
    fake_g_eval.reason = "ok"

    histo = metrics.EVAL_SCORE.labels(metric="g_eval", judge_model="llama3:8b")
    counter = metrics.EVAL_OUTCOMES_TOTAL.labels(
        metric="g_eval", judge_model="llama3:8b", outcome="passed",
    )
    histo_before = histo._sum.get()
    counter_before = counter._value.get()

    with patch.object(scoring, "is_enabled", return_value=True), \
         patch("deepeval.metrics.GEval", return_value=fake_g_eval), \
         patch("deepeval.models.OllamaModel"):
        scoring.score_response("a", "b", criteria="c")

    assert counter._value.get() - counter_before == 1
    assert histo._sum.get() - histo_before == pytest.approx(0.75)


def test_error_does_not_observe_score_histogram() -> None:
    """An error outcome bumps the counter but does NOT observe a 0.0
    on the score histogram. Otherwise judge failures would skew the
    p50/p95 percentiles toward zero on every dashboard."""
    from core import metrics

    config.EVAL_ENABLED = True

    # prometheus_client doesn't expose ._count on labelled histograms;
    # read the _count sample via collect() instead.
    def _count_for(metric_name: str, judge_model: str) -> float:
        for fam in metrics.EVAL_SCORE.collect():
            for sample in fam.samples:
                if (
                    sample.name.endswith("_count")
                    and sample.labels.get("metric") == metric_name
                    and sample.labels.get("judge_model") == judge_model
                ):
                    return float(sample.value)
        return 0.0

    before = _count_for("g_eval", "llama3:8b")

    with patch.object(scoring, "is_enabled", return_value=True), \
         patch("deepeval.models.OllamaModel", side_effect=RuntimeError("boom")):
        scoring.score_response("a", "b", criteria="c")

    assert _count_for("g_eval", "llama3:8b") == before
