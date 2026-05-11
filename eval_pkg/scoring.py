"""deepeval G-Eval wrapper with Ollama as the judge.

Single public entry point — ``score_response(input_text, actual_output,
*, criteria, judge_model=None, judge_base_url=None, threshold=None)``
— returns a typed ``EvalScore`` dataclass. The orchestrator never
calls this during a run; operators invoke it from the measurement
harness or from a downstream eval campaign.

Three-condition ``is_enabled()`` gate (config flag + deepeval
importable + ollama-python importable) mirrors the Phase 3.3
NoteDiscovery shape.

Failure handling: any unexpected error from deepeval (network, judge
hang, malformed response) returns an ``EvalScore`` with
``score=0.0``, ``passed=False``, ``reason=str(e)``, ``error=True``.
The Prom histogram is still observed so harness operators see the
failure on /metrics. The wrapper never raises out — eval is best-
effort observation, never a hard dependency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core import config as _config
from core.metrics import observe_eval_score

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvalScore:
    """Result of one G-Eval scoring call.

    ``score`` is in [0, 1]. ``passed`` is ``score >= threshold``.
    ``reason`` is the judge's natural-language rationale (may be
    empty on error). ``error`` is True when the wrapper trapped an
    exception and synthesised a zero-score result.
    """

    metric: str
    score: float
    passed: bool
    threshold: float
    judge_model: str
    reason: str
    error: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "score": self.score,
            "passed": self.passed,
            "threshold": self.threshold,
            "judge_model": self.judge_model,
            "reason": self.reason,
            "error": self.error,
        }


def is_enabled() -> bool:
    """Three-condition gate: config flag + deepeval + ollama importable."""
    if not _config.EVAL_ENABLED:
        return False
    try:
        import deepeval  # noqa: F401
        import ollama  # noqa: F401
    except ImportError:
        return False
    return True


def score_response(
    input_text: str,
    actual_output: str,
    *,
    criteria: str,
    metric_name: str = "g_eval",
    judge_model: str | None = None,
    judge_base_url: str | None = None,
    threshold: float | None = None,
) -> EvalScore:
    """Score one (input, actual_output) pair with G-Eval + Ollama judge.

    Returns a zero-score ``EvalScore`` with ``error=True`` when the
    gate is closed, when inputs are empty, or when any deepeval /
    judge call raises. Never raises out.
    """
    judge = judge_model or _config.EVAL_JUDGE_MODEL
    base = judge_base_url or _config.EVAL_JUDGE_BASE_URL
    thresh = float(threshold if threshold is not None else _config.EVAL_THRESHOLD)

    if not is_enabled():
        score = EvalScore(
            metric=metric_name,
            score=0.0,
            passed=False,
            threshold=thresh,
            judge_model=judge,
            reason="eval disabled (config flag, deepeval, or ollama missing)",
            error=True,
        )
        observe_eval_score(metric=metric_name, judge_model=judge, score=0.0, outcome="disabled")
        return score

    if not input_text or not actual_output:
        score = EvalScore(
            metric=metric_name,
            score=0.0,
            passed=False,
            threshold=thresh,
            judge_model=judge,
            reason="empty input or actual_output",
            error=True,
        )
        observe_eval_score(metric=metric_name, judge_model=judge, score=0.0, outcome="empty_input")
        return score

    # Imports are lazy so the module loads when deepeval isn't installed
    # (matches the dormant-ship pattern).
    from deepeval.metrics import GEval
    from deepeval.models import OllamaModel
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    try:
        ollama_judge = OllamaModel(model=judge, base_url=base, temperature=0.0)
        g_eval = GEval(
            name=metric_name,
            criteria=criteria,
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
            model=ollama_judge,
            threshold=thresh,
            async_mode=False,  # deterministic order for measurement
        )
        case = LLMTestCase(input=input_text, actual_output=actual_output)
        g_eval.measure(case)
        score_val = float(g_eval.score or 0.0)
        reason = str(g_eval.reason or "")
        result = EvalScore(
            metric=metric_name,
            score=score_val,
            passed=score_val >= thresh,
            threshold=thresh,
            judge_model=judge,
            reason=reason,
        )
        outcome = "passed" if result.passed else "failed"
        observe_eval_score(metric=metric_name, judge_model=judge, score=score_val, outcome=outcome)
        return result
    except Exception as exc:  # pragma: no cover - exercised via mocks in tests
        logger.warning("[eval.scoring] judge call failed: %s", exc)
        observe_eval_score(metric=metric_name, judge_model=judge, score=0.0, outcome="error")
        return EvalScore(
            metric=metric_name,
            score=0.0,
            passed=False,
            threshold=thresh,
            judge_model=judge,
            reason=f"judge error: {exc}",
            error=True,
        )
