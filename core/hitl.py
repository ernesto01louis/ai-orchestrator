"""Phase 3.1 HITL (human-in-the-loop) intervention infrastructure.

Five modes (per ``CampaignTemplate.hitl_mode``):

  full_auto    — today's behaviour; only Gates blocks pause
  gate_only    — Gate denials pause; otherwise auto
  checkpoint   — pauses at phase boundaries
                 (planner→generator→judge→optimizer)
  step_by_step — pauses after every LLM call (debug-only)
  co_pilot     — pauses BEFORE every LLM call to allow prompt edits

The orchestration loop calls ``hitl_checkpoint(...)`` at the right
moments; this module owns the bookkeeping (mode lookup, queue plumbing,
notification + block-and-wait helpers).

Phase 3.2's SmartPause uses the same lookup (``get_run_hitl_mode``) so
its threshold check goes live the moment 3.1 lands.
"""
from __future__ import annotations

import queue
import threading
from typing import Any

from core.config import HITL_DEFAULT_MODE, HITL_POLL_INTERVAL

VALID_HITL_MODES = (
    "full_auto",
    "gate_only",
    "checkpoint",
    "step_by_step",
    "co_pilot",
)


# Per-run intervention queue. Operators POST to /runs/{id}/intervene
# which calls ``post_intervention(run_id, payload)`` here; the
# orchestration loop's wait_for_intervention drains the queue. One
# slot is enough — interventions are point-events, not a stream.
INTERVENTION_QUEUE: dict[str, queue.Queue[dict[str, Any]]] = {}
_intervention_lock = threading.Lock()


def _ensure_queue(run_id: str) -> queue.Queue[dict[str, Any]]:
    """Lazy-create the per-run queue. Called by both the producer
    (``/runs/{id}/intervene``) and the consumer
    (``wait_for_intervention``)."""
    with _intervention_lock:
        q = INTERVENTION_QUEUE.get(run_id)
        if q is None:
            q = queue.Queue(maxsize=8)
            INTERVENTION_QUEUE[run_id] = q
        return q


def post_intervention(run_id: str, payload: dict[str, Any]) -> None:
    """Drop an operator-submitted intervention payload onto the run's
    queue. Non-blocking; raises ``queue.Full`` only if the operator
    spams faster than the orchestrator can drain (8-slot bound)."""
    _ensure_queue(run_id).put_nowait(payload)


def wait_for_intervention(
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = HITL_POLL_INTERVAL,
) -> dict[str, Any] | None:
    """Block until an intervention payload arrives or the deadline
    elapses. Returns the payload dict or ``None`` on timeout.

    The poll interval is here for symmetry with SmartPause; the
    underlying ``Queue.get(timeout=...)`` already handles the wait
    natively. Polling is left at the deadline boundary to keep
    deployment-pause / process-restart paths responsive.
    """
    q = _ensure_queue(run_id)
    try:
        return q.get(timeout=timeout_seconds)
    except Exception:
        # queue.Empty has no public super-class import; this catch
        # is intentionally broad (queue.get only raises Empty here).
        return None


def get_run_hitl_mode(run_id: str) -> str:
    """Return the campaign-level ``hitl_mode`` for a given ``run_id``.

    Replaces the Phase 3.2 stub. Lookup strategy:

      1. Linear scan of ``campaigns.json`` for a campaign whose
         ``runs[]`` contains ``run_id``.
      2. If found, return ``campaign.template.hitl_mode``
         (defaults to ``"full_auto"`` if the campaign was created
         before Phase 3.1 lands the field).
      3. Otherwise — single-shot orchestration with no parent
         campaign — return the system-wide ``HITL_DEFAULT_MODE``.

    Lazy-imports ``memory_pkg.load_campaigns`` to dodge the circular
    import with ``orchestration``. Linear scan is fine: this is only
    called at well-defined gate points (after planner, generator,
    judge, optimizer, or per-LLM-call in step_by_step mode), not in
    the hot path.
    """
    try:
        from memory_pkg import load_campaigns
    except Exception:
        return HITL_DEFAULT_MODE

    try:
        campaigns = load_campaigns()
    except Exception:
        return HITL_DEFAULT_MODE

    for campaign in campaigns.values():
        if not isinstance(campaign, dict):
            continue
        runs = campaign.get("runs") or []
        for run in runs:
            if isinstance(run, dict) and run.get("run_id") == run_id:
                template = campaign.get("template") or {}
                mode = template.get("hitl_mode") if isinstance(template, dict) else None
                if mode in VALID_HITL_MODES:
                    return str(mode)
                return HITL_DEFAULT_MODE

    return HITL_DEFAULT_MODE
