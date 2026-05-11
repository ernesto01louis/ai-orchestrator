#!/usr/bin/env python3
"""Measure deepeval-with-Ollama judge discrimination on a canned suite.

The repo-screening deepeval spike (2026-05-11) lands ``eval_pkg/scoring.py``
as an available primitive but does not wire it into the live pipeline.
This harness answers: **does G-Eval with a local Ollama judge correctly
distinguish good outputs from bad on a fixed canned suite?**

Suite is domain-neutral (per CLAUDE.md "generic platform" rule):
arithmetic, definition recall, instruction following.

Usage::

    python scripts/measure_eval_quality.py
    python scripts/measure_eval_quality.py --judge-model llama3:8b
    python scripts/measure_eval_quality.py --judge-model qwen2.5:72b \
        --out /tmp/eval-measurement.json

Exit code 0 when discrimination accuracy >= success-rate threshold
(default 0.8 — judges aren't perfect; an 80% bar weeds out a totally
broken pipeline without demanding perfection).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Allow running from the repo root without installing as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import config  # noqa: E402
from eval_pkg import scoring  # noqa: E402


@dataclass
class Case:
    name: str
    expected_pass: bool  # True if this output SHOULD pass G-Eval
    input_text: str
    actual_output: str
    criteria: str


# ---------------------------------------------------------------------------
# Canned suite — 8 cases (4 good, 4 bad) across 3 domains
# ---------------------------------------------------------------------------

SUITE: list[Case] = [
    # Arithmetic
    Case(
        name="arith_good",
        expected_pass=True,
        input_text="What is 2 + 2?",
        actual_output="The answer is 4.",
        criteria="Does the response correctly answer the arithmetic question?",
    ),
    Case(
        name="arith_bad",
        expected_pass=False,
        input_text="What is 2 + 2?",
        actual_output="The answer is 7.",
        criteria="Does the response correctly answer the arithmetic question?",
    ),
    # Definition recall
    Case(
        name="def_good",
        expected_pass=True,
        input_text="What does HTTP stand for?",
        actual_output="HTTP stands for Hypertext Transfer Protocol.",
        criteria="Does the response correctly expand the acronym?",
    ),
    Case(
        name="def_bad",
        expected_pass=False,
        input_text="What does HTTP stand for?",
        actual_output="HTTP stands for High Tension Telephone Pole.",
        criteria="Does the response correctly expand the acronym?",
    ),
    # Instruction following
    Case(
        name="instr_good",
        expected_pass=True,
        input_text="List three primary colors.",
        actual_output="Red, blue, and yellow.",
        criteria=(
            "Does the response list exactly three primary colors, and are "
            "they actually primary colors?"
        ),
    ),
    Case(
        name="instr_bad_count",
        expected_pass=False,
        input_text="List three primary colors.",
        actual_output="Red.",
        criteria=(
            "Does the response list exactly three primary colors, and are "
            "they actually primary colors?"
        ),
    ),
    Case(
        name="instr_bad_content",
        expected_pass=False,
        input_text="List three primary colors.",
        actual_output="Purple, orange, and green.",
        criteria=(
            "Does the response list exactly three primary colors, and are "
            "they actually primary colors?"
        ),
    ),
    # Refusal / off-topic
    Case(
        name="refuse_bad",
        expected_pass=False,
        input_text="What is the capital of France?",
        actual_output="I cannot answer that question.",
        criteria="Does the response factually answer the geography question?",
    ),
]


def _run_case(case: Case, judge_model: str, judge_base_url: str) -> dict:
    started = time.monotonic()
    score = scoring.score_response(
        case.input_text,
        case.actual_output,
        criteria=case.criteria,
        metric_name=case.name,
        judge_model=judge_model,
        judge_base_url=judge_base_url,
    )
    elapsed = time.monotonic() - started
    correct = score.passed == case.expected_pass
    return {
        "case": case.name,
        "expected_pass": case.expected_pass,
        "actual_pass": score.passed,
        "score": score.score,
        "reason": score.reason,
        "correct": correct,
        "error": score.error,
        "elapsed_seconds": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judge-model", default=None,
                        help="Ollama judge model (default: from config.json).")
    parser.add_argument("--judge-base-url", default=None,
                        help="Ollama judge base URL (default: from config.json).")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="G-Eval pass threshold (default: 0.5).")
    parser.add_argument(
        "--success-rate", type=float, default=0.8,
        help="Discrimination accuracy required for exit code 0 (default: 0.8).",
    )
    parser.add_argument("--out", type=Path, default=None,
                        help="Write full per-case JSON to this path.")
    parser.add_argument(
        "--cases", default=None,
        help="Comma-separated list of case names to run (default: all).",
    )
    args = parser.parse_args()

    # Force-enable the eval primitive for the measurement run. Does not
    # write back to config.json.
    config.EVAL_ENABLED = True
    if args.judge_model:
        config.EVAL_JUDGE_MODEL = args.judge_model
    if args.judge_base_url:
        config.EVAL_JUDGE_BASE_URL = args.judge_base_url
    config.EVAL_THRESHOLD = args.threshold

    suite = SUITE
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        suite = [c for c in SUITE if c.name in wanted]
        if not suite:
            print(f"[measure] no cases matched {wanted!r}", file=sys.stderr)
            return 2

    print(
        f"[measure] running {len(suite)} cases against "
        f"{config.EVAL_JUDGE_MODEL} at {config.EVAL_JUDGE_BASE_URL}",
        file=sys.stderr,
    )

    per_case: list[dict] = []
    for case in suite:
        result = _run_case(case, config.EVAL_JUDGE_MODEL, config.EVAL_JUDGE_BASE_URL)
        per_case.append(result)
        marker = "✓" if result["correct"] else "✗"
        print(
            f"  {marker} {case.name:18s} expected={case.expected_pass} "
            f"actual={result['actual_pass']} score={result['score']:.3f} "
            f"({result['elapsed_seconds']:.1f}s)",
            file=sys.stderr,
        )

    correct = sum(1 for r in per_case if r["correct"])
    total = len(per_case)
    errors = sum(1 for r in per_case if r["error"])
    accuracy = correct / total if total else 0.0
    total_time = sum(r["elapsed_seconds"] for r in per_case)

    aggregate = {
        "judge_model": config.EVAL_JUDGE_MODEL,
        "judge_base_url": config.EVAL_JUDGE_BASE_URL,
        "threshold": config.EVAL_THRESHOLD,
        "cases_total": total,
        "cases_correct": correct,
        "cases_errored": errors,
        "discrimination_accuracy": accuracy,
        "total_wallclock_seconds": total_time,
        "mean_wallclock_seconds_per_case": (
            total_time / total if total else 0.0
        ),
        "spike_criterion_met": accuracy >= args.success_rate,
    }
    out_blob = {"aggregate": aggregate, "per_case": per_case}

    if args.out:
        args.out.write_text(json.dumps(out_blob, indent=2))
        print(f"[measure] wrote {args.out}", file=sys.stderr)

    print(json.dumps(aggregate, indent=2))
    return 0 if aggregate["spike_criterion_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
