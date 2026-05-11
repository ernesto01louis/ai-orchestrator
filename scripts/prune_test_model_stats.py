#!/usr/bin/env python3
"""Prune test-fixture pollution from ``memory/model_stats.json``.

The orchestrator test suite (``tests/test_orchestrate_flow.py``,
``tests/test_smartpause.py``) uses ``m1`` / ``m2`` / ``m3`` as
generator-model names with deterministic always-win / always-fail
behaviour. When those tests run against the live FastAPI app (via the
session-scoped ``inprocess_client`` TestClient), the orchestration loop
records their outcomes into the live ``memory/model_stats.json``.

Those entries skew the "promote winners / retire losers" review on the
ROADMAP's monthly hygiene cycle (they show m1 at 100% win on 3500+ runs).

This script removes the test-name entries with proper file locking and
prints a diff. Idempotent — re-runs are no-ops once the pollution is
gone. Drop new test-fixture model names into ``TEST_MODEL_NAMES``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add the orchestrator root to sys.path so we can use its locks helper.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.locks import locked_read_json, locked_write_json  # noqa: E402
from core.paths import MODEL_STATS  # noqa: E402

TEST_MODEL_NAMES = {"m1", "m2", "m3"}


def main() -> int:
    stats = locked_read_json(MODEL_STATS, default={})
    if not isinstance(stats, dict):
        print(f"[prune] unexpected type {type(stats).__name__} in {MODEL_STATS}", file=sys.stderr)
        return 2

    polluted = sorted(k for k in stats if k in TEST_MODEL_NAMES)
    if not polluted:
        print(f"[prune] {MODEL_STATS} is clean (no test-fixture entries).")
        return 0

    print(f"[prune] removing {len(polluted)} test-fixture entries from {MODEL_STATS}:")
    for model in polluted:
        s = stats[model]
        runs = s.get("total_runs") if isinstance(s, dict) else "?"
        wins = s.get("wins") if isinstance(s, dict) else "?"
        print(f"        - {model}: total_runs={runs} wins={wins}")
        del stats[model]

    locked_write_json(MODEL_STATS, stats)
    print(f"[prune] {MODEL_STATS} updated. {len(stats)} models remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
