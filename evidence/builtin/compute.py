"""Compute-resources calculator.

NeurIPS Q8 mandates reporting compute resources. We emit per-run
wall-clock duration plus campaign totals: total wall-clock,
total LLM call count, total code-execution count, mean LLM latency,
and an estimated total token count (sum of ``response_tokens`` across
LLM calls — a lower bound, since prompt tokens aren't tracked yet).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.evidence import CalculatorResult
from evidence.hookspecs import hookimpl

if TYPE_CHECKING:
    from core.campaign import Campaign
    from core.evidence import RunRecord


_CALCULATOR_ID = "ai_orchestrator.builtin.compute:v1"
_OUTPUT_SCHEMA_VERSION = "1.0.0"


@hookimpl
def compute_evidence(
    campaign: "Campaign", runs: "list[RunRecord]"
) -> list[CalculatorResult]:
    started = time.monotonic()

    per_run_seconds = [
        max(0.0, (r.finished_at - r.started_at).total_seconds()) for r in runs
    ]
    total_wall_clock = sum(per_run_seconds)

    llm_call_count = sum(len(r.llm_calls) for r in runs)
    code_execution_count = sum(len(r.code_executions) for r in runs)

    all_latencies = [c.latency_ms for r in runs for c in r.llm_calls]
    mean_latency_ms = (sum(all_latencies) / len(all_latencies)) if all_latencies else 0.0

    response_tokens_lower_bound = sum(
        c.response_tokens for r in runs for c in r.llm_calls
    )

    output = {
        "n_runs": len(runs),
        "total_wall_clock_seconds": total_wall_clock,
        "per_run_seconds": per_run_seconds,
        "llm_call_count": llm_call_count,
        "code_execution_count": code_execution_count,
        "mean_llm_latency_ms": mean_latency_ms,
        "response_tokens_lower_bound": response_tokens_lower_bound,
    }

    duration_ms = int((time.monotonic() - started) * 1000)
    return [
        CalculatorResult(
            kind="compute_resources",
            calculator_id=_CALCULATOR_ID,
            schema_version=_OUTPUT_SCHEMA_VERSION,
            inputs={"campaign_id": campaign.id, "n_runs": len(runs)},
            output=output,
            duration_ms=duration_ms,
            deterministic=True,
        )
    ]
