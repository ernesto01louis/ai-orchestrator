"""Phase 2.4 — budget tracking primitives.

Pure functions: cost computation, threshold-state transitions. No I/O,
no module-level state. Callers (state hooks, write-through, routes)
import these helpers and apply the results to whatever store they own.

The single source of truth for rate values is ``core.config.BUDGET_RATES``.
``cost_usd_for`` performs a simple lookup-with-fallback to the
``"default"`` entry — operators can override per-model rates without
touching code.

Threshold transitions are computed by ``evaluate_thresholds`` from a
running ``budget_used_usd`` / ``budget_total_usd`` pair plus the list of
already-emitted thresholds. Output is a structured ``BudgetEval``
record describing what state the campaign should be in and which (if
any) new threshold the caller should fire a notification for.

``accrue_to_campaign`` is the single side-effecting entry point: it
reads + writes the JSON-canonical campaign state, fires notifications
for newly-crossed thresholds, and pauses the campaign when 100% is
breached. Called from the on_task_completion state hook AFTER the
LlmCall has been recorded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

# Public state values, also enforced by the Postgres CHECK constraint
# in alembic/versions/0002_budget_tracking.py.
STATE_OK = "ok"
STATE_WARNING = "warning"
STATE_BREACH = "breach"
STATE_PAUSED = "paused"


def cost_usd_for(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    """Return USD cost for a single LLM call.

    ``prompt_tokens`` and ``completion_tokens`` are charged separately
    against the per-model rate (USD per 1M tokens). Unknown models fall
    back to the ``"default"`` rate. Negative or zero token counts are
    clamped to ``0`` — never charge for what wasn't sent.
    """
    from core import config  # noqa: PLC0415

    rates = config.BUDGET_RATES
    rate = rates.get(model) or rates.get("default") or {}
    prompt_rate = float(rate.get("prompt", 0.0) or 0.0)
    completion_rate = float(rate.get("completion", 0.0) or 0.0)

    p = max(int(prompt_tokens or 0), 0)
    c = max(int(completion_tokens or 0), 0)

    return (p * prompt_rate + c * completion_rate) / 1_000_000.0


@dataclass(frozen=True)
class BudgetEval:
    """Result of a single budget-state evaluation.

    Fields:
    * ``state`` — one of ``ok`` / ``warning`` / ``breach`` (the caller
      promotes ``breach`` → ``paused`` once they've actually paused
      the campaign — the eval itself doesn't perform side-effects).
    * ``newly_crossed`` — list of int percentages whose threshold the
      caller should fire a notification for. Empty when no threshold
      crossed since the last evaluation.
    * ``thresholds_emitted`` — superset of the input ``thresholds_emitted``,
      with any ``newly_crossed`` percentages appended. Caller persists
      this back to the Campaign record so a re-evaluation doesn't
      double-fire.
    * ``should_pause`` — convenience: ``True`` iff ``100`` is in
      ``newly_crossed`` (and was therefore not already emitted).
    """

    state: str
    newly_crossed: list[int]
    thresholds_emitted: list[int]
    should_pause: bool


def evaluate_thresholds(
    budget_used_usd: float,
    budget_total_usd: float | None,
    thresholds_pct: list[int],
    thresholds_emitted: list[int],
) -> BudgetEval:
    """Compute the next budget state given current usage.

    No total → state stays ``ok`` no matter how much was spent
    (operators who never set a budget never get auto-paused). With a
    total set, the state ramps up through ``warning`` (anything above
    the lowest threshold but below 100%) to ``breach`` (≥100%).

    The caller is responsible for any side-effects: persisting the new
    ``thresholds_emitted``, sending notifications for each entry in
    ``newly_crossed``, and pausing the campaign when ``should_pause``
    is ``True``.
    """
    if budget_total_usd is None or budget_total_usd <= 0:
        return BudgetEval(
            state=STATE_OK,
            newly_crossed=[],
            thresholds_emitted=list(thresholds_emitted),
            should_pause=False,
        )

    pct = (budget_used_usd / budget_total_usd) * 100.0
    sorted_thresholds = sorted(set(thresholds_pct))

    crossed_now: list[int] = [t for t in sorted_thresholds if pct >= t]
    newly_crossed = [t for t in crossed_now if t not in thresholds_emitted]

    if pct >= 100:
        state = STATE_BREACH
    elif crossed_now:
        state = STATE_WARNING
    else:
        state = STATE_OK

    return BudgetEval(
        state=state,
        newly_crossed=newly_crossed,
        thresholds_emitted=sorted(set(thresholds_emitted) | set(newly_crossed)),
        should_pause=100 in newly_crossed,
    )


def percentage_used(
    budget_used_usd: float, budget_total_usd: float | None
) -> float | None:
    """Convenience: return percent-of-total or ``None`` when no total set."""
    if budget_total_usd is None or budget_total_usd <= 0:
        return None
    return (budget_used_usd / budget_total_usd) * 100.0


# ---------------------------------------------------------------------------
# Side-effecting accrual entry point
# ---------------------------------------------------------------------------


def _find_campaign_for_run(
    run_id: str, campaigns_map: dict[str, dict[str, Any]]
) -> str | None:
    """Scan ``campaigns_map`` for the campaign owning ``run_id``.

    Linear scan over the at-most-few-thousand campaigns. Cheap enough
    on the LLM-call path (per-call latency dominated by network) but
    callers should still cache the lookup if they're hot.
    """
    for cid, record in campaigns_map.items():
        for run in record.get("runs", []) or []:
            if isinstance(run, dict) and run.get("run_id") == run_id:
                return cid
    return None


def accrue_to_campaign(run_id: str, cost_delta: float) -> None:
    """Apply ``cost_delta`` USD to whichever campaign owns ``run_id``.

    Pipeline:
    1. Skip when budget tracking is disabled OR ``cost_delta`` is zero.
    2. Find the owning campaign (linear scan of ``memory/campaigns.json``).
       Runs with no campaign are silently skipped.
    3. Read current ``budget_used_usd`` / ``budget_total_usd`` /
       ``budget_state`` / ``budget_thresholds_emitted``.
    4. Add ``cost_delta`` to ``budget_used_usd``.
    5. Re-evaluate thresholds; emit notifications for each newly-crossed
       percentage; bump the Prometheus counter.
    6. If the eval flags ``should_pause``, pause the campaign via the
       same path as the manual ``/campaigns/{id}/pause`` route.
    7. Persist the updated record back via ``save_campaigns`` (which
       also dual-writes to Postgres in Phase 2.1 mode).

    Failure-tolerant: any unexpected error logs + returns. The state
    hook calling us must never raise out of a Prefect ``@task`` body.
    """
    try:
        from core import config  # noqa: PLC0415
    except ImportError:
        return
    if not config.BUDGET_ENABLED or not cost_delta:
        return
    try:
        from memory_pkg import load_campaigns, save_campaigns  # noqa: PLC0415
    except Exception as exc:
        _logger.warning("budget_accrue_load_failed run_id=%s error=%s", run_id, exc)
        return

    try:
        campaigns_map = load_campaigns()  # type: ignore[no-untyped-call]
    except Exception as exc:
        _logger.warning(
            "budget_accrue_load_campaigns_failed run_id=%s error=%s", run_id, exc,
        )
        return

    cid = _find_campaign_for_run(run_id, campaigns_map)
    if cid is None:
        return
    record = campaigns_map[cid]

    used_before = float(record.get("budget_used_usd", 0.0) or 0.0)
    total = record.get("budget_total_usd")
    total_f = float(total) if total is not None else None
    emitted = list(record.get("budget_thresholds_emitted", []) or [])

    used_after = used_before + float(cost_delta)
    record["budget_used_usd"] = used_after

    eval_result = evaluate_thresholds(
        budget_used_usd=used_after,
        budget_total_usd=total_f,
        thresholds_pct=list(config.BUDGET_THRESHOLDS_PCT),
        thresholds_emitted=emitted,
    )
    record["budget_state"] = eval_result.state
    record["budget_thresholds_emitted"] = eval_result.thresholds_emitted

    # Notifications + metrics for each newly-crossed threshold.
    for pct in eval_result.newly_crossed:
        _notify_threshold(cid, pct, used_after, total_f)
        _observe_threshold(pct, eval_result.state)

    # Auto-pause on 100% breach. Promote the JSON state to ``paused``
    # AFTER the pause call succeeds so a partially-paused campaign is
    # still visibly in ``breach`` — operators can tell which is which.
    if eval_result.should_pause:
        try:
            paused = _pause_campaign(cid)
            if paused:
                record["budget_state"] = STATE_PAUSED
        except Exception as exc:
            _logger.warning(
                "budget_pause_failed campaign_id=%s error=%s", cid, exc,
            )

    try:
        save_campaigns(  # type: ignore[no-untyped-call]
            campaigns_map, changed_ids={cid},
        )
    except Exception as exc:
        _logger.warning(
            "budget_accrue_save_failed campaign_id=%s error=%s", cid, exc,
        )


def _notify_threshold(
    campaign_id: str, pct: int, used_usd: float, total_usd: float | None,
) -> None:
    """Fire a Gotify/ntfy notification for a threshold crossing.

    Best-effort — notifications.send is itself failure-tolerant, but
    we belt-and-braces the import so a missing module never blocks.
    """
    try:
        from notifications.send import send_notification  # noqa: PLC0415
    except Exception:
        return
    severity = "warning" if pct < 100 else "critical"
    title = f"Budget {pct}% — {severity}"
    total_str = f"${total_usd:.2f}" if total_usd is not None else "(no total)"
    message = (
        f"Campaign {campaign_id}: ${used_usd:.4f} / {total_str}. "
        f"{'Auto-paused.' if pct >= 100 else 'Approaching budget.'}"
    )
    priority = 8 if pct >= 100 else 5
    try:
        send_notification(  # type: ignore[no-untyped-call]
            title=title, message=message, priority=priority,
        )
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning(
            "budget_notify_failed campaign_id=%s pct=%s error=%s",
            campaign_id, pct, exc,
        )


def _observe_threshold(pct: int, state: str) -> None:
    """Bump the budget Prom counter without dragging metrics module
    in if it's failing for any reason."""
    try:
        from core.metrics import observe_budget_threshold  # noqa: PLC0415
        observe_budget_threshold(pct, state)
    except Exception:
        pass


def _pause_campaign(campaign_id: str) -> bool:
    """Pause a campaign because its budget breached 100%.

    Mirrors the manual ``/campaigns/{id}/pause`` route: flips the
    in-process ``CAMPAIGN_STATUS`` flag and signals Prefect to pause
    the flow_run (when one is active). Returns ``True`` on success.
    """
    try:
        from core.runtime import CAMPAIGN_STATUS  # noqa: PLC0415
    except Exception:
        return False
    CAMPAIGN_STATUS.setdefault(campaign_id, {})["paused"] = True

    try:
        from prefect_io import pause_flow_run  # noqa: PLC0415
    except Exception:
        return True  # in-process flag flip already succeeded
    try:
        flow_run_id = CAMPAIGN_STATUS.get(campaign_id, {}).get("flow_run_id")
        if flow_run_id:
            pause_flow_run(flow_run_id)
    except Exception as exc:  # pragma: no cover — Prefect-down fallback
        _logger.warning(
            "budget_pause_prefect_failed campaign_id=%s error=%s",
            campaign_id, exc,
        )
    return True
