"""Phase 2.6 — operator-console structured metrics endpoint.

The Prometheus ``/metrics`` route at ``api/routes/health.py`` returns
text-format exposition for scraping. The operator console UI needs a
JSON-shaped sibling so a single ``react-query`` fetch can populate the
Dashboard metric cards + sparklines without parsing Prometheus textfile.

This handler is read-only. It pulls from:

* ``core.metrics`` global Counter / Gauge / Histogram instruments
  (sum across labels for whole-system totals)
* ``RUN_STATUS`` for in-flight counts (active / paused)
* ``memory_pkg.load_campaigns()`` for campaign + budget aggregates

Fields that depend on a sliding window (``llm_calls_rate_5m``, sparkline
arrays, percentile latencies) are best-effort: when the metric
infrastructure can't produce the value (eg. histogram buckets too coarse
for p99) the field is emitted as 0 / null and the dashboard renders the
"—" placeholder. This is intentional — the goal is "every panel has a
number" not "every number is precise."
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from core import metrics as _core_metrics
from core.runtime import RUN_STATUS
from memory_pkg import load_campaigns

router = APIRouter()


def _sum_counter(counter) -> float:
    """Sum all samples of a Prom Counter across labels.

    Returns 0.0 when the counter has never been incremented.
    """
    total = 0.0
    try:
        for metric in counter.collect():
            for sample in metric.samples:
                if sample.name.endswith("_total"):
                    total += float(sample.value)
    except Exception:
        return 0.0
    return total


def _histogram_quantile(histogram, q: float) -> float:
    """Compute approximate quantile from a Prom Histogram's bucket counts.

    Returns 0.0 when there are no observations or the histogram is
    misconfigured. q in [0, 1].
    """
    try:
        buckets: list[tuple[float, float]] = []  # (upper, cumulative_count)
        total_count = 0.0
        for metric in histogram.collect():
            for sample in metric.samples:
                if sample.name.endswith("_bucket"):
                    le = sample.labels.get("le")
                    if le is None:
                        continue
                    upper = float("inf") if le == "+Inf" else float(le)
                    buckets.append((upper, float(sample.value)))
                elif sample.name.endswith("_count"):
                    total_count = max(total_count, float(sample.value))
        if total_count <= 0 or not buckets:
            return 0.0
        # Buckets in a single Histogram are already monotone increasing;
        # `_count` matches the last bucket's cumulative.
        buckets.sort(key=lambda b: b[0])
        target = q * total_count
        for upper, count in buckets:
            if count >= target and upper != float("inf"):
                return upper
    except Exception:
        return 0.0
    return 0.0


@router.get("/metrics.json")
def metrics_json() -> dict[str, Any]:
    """Return a JSON-shaped metric snapshot for the operator console.

    Shape mirrors ``ui/console/src/lib/types.ts:Metrics``.
    """
    # ── LLM activity ──────────────────────────────────────────────
    llm_calls_total = int(_sum_counter(_core_metrics.LLM_CALLS_TOTAL))
    # Agent-task histogram in seconds → convert to ms for the dashboard.
    p50_s = _histogram_quantile(_core_metrics.AGENT_TASK_SECONDS, 0.50)
    p95_s = _histogram_quantile(_core_metrics.AGENT_TASK_SECONDS, 0.95)
    p99_s = _histogram_quantile(_core_metrics.AGENT_TASK_SECONDS, 0.99)

    # ── Runs ─────────────────────────────────────────────────────
    active_runs = 0
    paused_runs = 0
    for info in RUN_STATUS.values():
        if not info.get("completed"):
            active_runs += 1
            if info.get("paused"):
                paused_runs += 1

    # ── Campaigns + budget aggregate ─────────────────────────────
    campaigns = load_campaigns()
    campaigns_active = sum(
        1 for c in campaigns.values()
        if c.get("status") in ("running", "active", "paused")
    )
    budget_used_total = 0.0
    budget_total_total = 0.0
    for c in campaigns.values():
        used = c.get("budget_used_usd")
        total = c.get("budget_total_usd")
        if isinstance(used, (int, float)):
            budget_used_total += float(used)
        if isinstance(total, (int, float)):
            budget_total_total += float(total)

    return {
        # ── LLM ──
        "llm_calls_total": llm_calls_total,
        # Rate over the last 5m would require either a /metrics scrape
        # ratepool or a Prefect-side rolling window. Today the orchestrator
        # only keeps the cumulative counter, so emit 0 and let the UI
        # render "—" or a flat sparkline. Wiring this up to a real rolling
        # window is a small follow-up; the dashboard remains useful in
        # the meantime via the cumulative + p95 fields.
        "llm_calls_rate_5m": 0.0,
        "llm_tokens_in_total": 0,
        "llm_tokens_out_total": 0,
        "llm_p50_ms": int(p50_s * 1000),
        "llm_p95_ms": int(p95_s * 1000),
        "llm_p99_ms": int(p99_s * 1000),
        # ── Runs / campaigns ──
        "campaigns_active": campaigns_active,
        "runs_active": active_runs,
        "runs_paused": paused_runs,
        # ── Budget ──
        "budget_total_usd": budget_total_total,
        "budget_used_usd": budget_used_total,
        # ── Ollama (these would come from a Prometheus scrape of the
        # Ollama servers; the orchestrator doesn't track them directly.
        # Emit zeros so the dashboard cards still render — operators
        # who want real values wire an Ollama exporter and update the
        # source here.)
        "ollama_queue_depth": 0,
        "ollama_gpu_util": 0.0,
        "ollama_vram_used_gb": 0.0,
        "ollama_vram_total_gb": 0.0,
    }
