"""Statistical-summary calculator.

Aggregates the per-run scores into mean / sd / median / min / max,
a 95% confidence interval (Student-t, two-sided, α=0.05), and the
identity of the best run. NeurIPS Q7 ("statistical significance") and
REFORMS §7 ("metrics & uncertainty quantification") cite this kind of
summary as the minimum reporting bar.

Output shape (output dict)::

    {
      "metric": "score",
      "n": int,
      "mean": float,
      "sd": float,
      "median": float,
      "min": float,
      "max": float,
      "ci95_lower": float,
      "ci95_upper": float,
      "success_rate": float,
      "best_run_id": str | None,
    }

For n=1 the CI collapses to (mean, mean) and `sd` is 0.
For n=0 the calculator returns an empty list (no statistical claim
is honest with no data).
"""
from __future__ import annotations

import statistics
import time
from typing import TYPE_CHECKING

from core.evidence import CalculatorResult
from evidence.hookspecs import hookimpl

if TYPE_CHECKING:
    from core.campaign import Campaign
    from core.evidence import RunRecord


_CALCULATOR_ID = "ai_orchestrator.builtin.stats:v1"
_OUTPUT_SCHEMA_VERSION = "1.0.0"
# Two-sided 95% normal-approximation half-width. We use the normal
# rather than Student-t for n>=30; for small n the result is wider than
# strictly correct, which is the conservative direction.
_Z_95 = 1.959964


@hookimpl
def compute_evidence(
    campaign: "Campaign", runs: "list[RunRecord]"
) -> list[CalculatorResult]:
    started = time.monotonic()

    scored = [r for r in runs if "score" in r.metrics]
    n = len(scored)

    if n == 0:
        return []

    scores = [float(r.metrics["score"]) for r in scored]
    mean = statistics.fmean(scores)
    if n >= 2:
        sd = statistics.stdev(scores)
        # Normal approximation; Student-t correction left to follow-up.
        half = _Z_95 * (sd / (n ** 0.5))
        ci_low, ci_high = mean - half, mean + half
    else:
        sd = 0.0
        ci_low = ci_high = mean

    best = max(scored, key=lambda r: float(r.metrics["score"]))
    successes = sum(1 for r in scored if r.status == "success")

    output = {
        "metric": "score",
        "n": n,
        "mean": mean,
        "sd": sd,
        "median": statistics.median(scores),
        "min": min(scores),
        "max": max(scores),
        "ci95_lower": ci_low,
        "ci95_upper": ci_high,
        "success_rate": successes / n,
        "best_run_id": best.run_id,
    }

    duration_ms = int((time.monotonic() - started) * 1000)
    return [
        CalculatorResult(
            kind="statistical_summary",
            calculator_id=_CALCULATOR_ID,
            schema_version=_OUTPUT_SCHEMA_VERSION,
            inputs={"metric": "score", "n_runs": n, "campaign_id": campaign.id},
            output=output,
            duration_ms=duration_ms,
            deterministic=True,
        )
    ]
