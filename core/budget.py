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
"""
from __future__ import annotations

from dataclasses import dataclass

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
