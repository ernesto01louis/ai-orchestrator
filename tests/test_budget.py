"""Tests for core.budget — cost calculator + threshold transitions."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from core import budget, config


@pytest.fixture
def example_rates(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a minimal rate table that exercises both lookup paths."""
    monkeypatch.setattr(
        config,
        "BUDGET_RATES",
        {
            "default": {"prompt": 0.5, "completion": 1.5},
            "qwen2.5:72b": {"prompt": 0.0, "completion": 0.0},
            "claude-opus-4-7": {"prompt": 15.0, "completion": 75.0},
        },
        raising=False,
    )
    yield


# ---------------------------------------------------------------------------
# cost_usd_for
# ---------------------------------------------------------------------------


def test_cost_zero_for_local_model(example_rates: None) -> None:
    """qwen2.5:72b is configured with zero rates — cost is always 0."""
    assert budget.cost_usd_for("qwen2.5:72b", 1_000_000, 1_000_000) == 0.0


def test_cost_paid_model_per_million_tokens(example_rates: None) -> None:
    """claude-opus-4-7: $15 per 1M prompt + $75 per 1M completion."""
    # 1M prompt @ $15 + 1M completion @ $75 = $90
    assert budget.cost_usd_for("claude-opus-4-7", 1_000_000, 1_000_000) == 90.0


def test_cost_partial_million(example_rates: None) -> None:
    """Fractional millions scale linearly."""
    # 250k prompt @ $15/M = $3.75; 100k completion @ $75/M = $7.5; total $11.25
    cost = budget.cost_usd_for("claude-opus-4-7", 250_000, 100_000)
    assert cost == pytest.approx(11.25)


def test_cost_unknown_model_uses_default(example_rates: None) -> None:
    """An unmapped model falls back to ``default``."""
    # 1M prompt @ $0.5 + 1M completion @ $1.5 = $2
    assert budget.cost_usd_for("never-heard-of-it", 1_000_000, 1_000_000) == 2.0


def test_cost_negative_clamped_to_zero(example_rates: None) -> None:
    """Negative token counts must not produce negative cost."""
    assert budget.cost_usd_for("claude-opus-4-7", -100, -50) == 0.0


def test_cost_zero_when_default_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty rate map produces zero cost — never raises."""
    monkeypatch.setattr(config, "BUDGET_RATES", {}, raising=False)
    assert budget.cost_usd_for("claude-opus-4-7", 1_000_000, 1_000_000) == 0.0


# ---------------------------------------------------------------------------
# evaluate_thresholds
# ---------------------------------------------------------------------------


def test_evaluate_no_total_stays_ok() -> None:
    """No budget set means the campaign never breaches."""
    result = budget.evaluate_thresholds(
        budget_used_usd=999.0,
        budget_total_usd=None,
        thresholds_pct=[50, 80, 100],
        thresholds_emitted=[],
    )
    assert result.state == "ok"
    assert result.newly_crossed == []
    assert result.should_pause is False


def test_evaluate_below_first_threshold() -> None:
    """40% of $100 → state ok, no thresholds crossed."""
    result = budget.evaluate_thresholds(40.0, 100.0, [50, 80, 100], [])
    assert result.state == "ok"
    assert result.newly_crossed == []


def test_evaluate_crosses_first_threshold() -> None:
    """55% → state warning, 50 newly crossed."""
    result = budget.evaluate_thresholds(55.0, 100.0, [50, 80, 100], [])
    assert result.state == "warning"
    assert result.newly_crossed == [50]
    assert result.thresholds_emitted == [50]


def test_evaluate_idempotent_after_emit() -> None:
    """Re-evaluating after we already emitted 50% must not re-fire."""
    result = budget.evaluate_thresholds(55.0, 100.0, [50, 80, 100], [50])
    assert result.state == "warning"
    assert result.newly_crossed == []
    assert result.thresholds_emitted == [50]


def test_evaluate_crosses_breach_and_pauses() -> None:
    """≥100% → state breach + should_pause."""
    result = budget.evaluate_thresholds(105.0, 100.0, [50, 80, 100], [])
    assert result.state == "breach"
    # We crossed 50 / 80 / 100 in one step; all three are newly crossed.
    assert result.newly_crossed == [50, 80, 100]
    assert result.should_pause is True


def test_evaluate_breach_only_pauses_once() -> None:
    """When 100 was already emitted, should_pause stays False."""
    result = budget.evaluate_thresholds(105.0, 100.0, [50, 80, 100], [50, 80, 100])
    assert result.state == "breach"
    assert result.newly_crossed == []
    assert result.should_pause is False


def test_evaluate_thresholds_unsorted_input_normalises() -> None:
    """Operators sometimes write [80, 50, 100] — output stays sorted."""
    result = budget.evaluate_thresholds(85.0, 100.0, [80, 50, 100], [])
    assert result.newly_crossed == [50, 80]
    assert result.thresholds_emitted == [50, 80]


# ---------------------------------------------------------------------------
# percentage_used
# ---------------------------------------------------------------------------


def test_percentage_used_none_when_no_total() -> None:
    assert budget.percentage_used(50.0, None) is None


def test_percentage_used_normal() -> None:
    assert budget.percentage_used(25.0, 100.0) == 25.0


def test_percentage_used_can_exceed_100() -> None:
    """Crossings happen at exactly 100, but a runaway campaign can go higher."""
    assert budget.percentage_used(150.0, 100.0) == 150.0
