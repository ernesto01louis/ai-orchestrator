"""Repo-screening spike (2026-05-11): deepeval-backed LLM output eval.

Named ``eval_pkg`` to avoid shadowing Python's builtin ``eval()`` (and
matches the codebase's ``references_pkg`` / ``memory_pkg`` convention
for collision-prone names).

Ships dormant. Public surface::

    from eval_pkg.scoring import score_response, EvalScore

    score = score_response(
        input_text="What is 2 + 2?",
        actual_output="The answer is 4.",
        criteria="Does the response correctly answer the arithmetic question?",
    )
    print(score.score, score.passed, score.reason)

The orchestrator never invokes ``score_response`` during a run — eval
runs explicitly from the operator-facing measurement harness
(``scripts/measure_eval_quality.py``) or from a downstream eval
campaign once one is built.
"""
from __future__ import annotations

from eval_pkg.scoring import EvalScore, is_enabled, score_response

__all__ = ["EvalScore", "is_enabled", "score_response"]
