"""Tests for the 5 builtin evidence calculators."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.campaign import Campaign, CampaignTemplate
from core.evidence import RunRecord
from evidence import get_plugin_manager, reset_plugin_manager


@pytest.fixture(autouse=True)
def _reset_plugins():
    """Each test sees a clean PluginManager."""
    reset_plugin_manager()
    yield
    reset_plugin_manager()


@pytest.fixture
def campaign() -> Campaign:
    return Campaign(
        id="C1", name="calc-test", hypothesis="seeds yield comparable scores",
        template=CampaignTemplate(
            project_name="p", prompt="go", planner_model="m",
            generator_models=["m"], judge_model="m", deploy_target="pi-1",
        ),
        params={"seed": [1, 2, 3]},
        status="completed", runs=[],
        created_at="2026-05-03T10:00:00", updated_at="2026-05-03T10:30:00",
    )


def _make_runs(scores: list[float]) -> list[RunRecord]:
    return [
        RunRecord(
            run_id=f"r{i}", parameters={"seed": i},
            metrics={"score": score}, status="success",
            started_at=datetime(2026, 5, 3, 12, i, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 5, 3, 12, i, 30, tzinfo=timezone.utc),
        )
        for i, score in enumerate(scores, 1)
    ]


def _flatten(nested):
    return [r for sub in nested for r in sub]


def test_all_five_builtins_register_and_fire(campaign):
    pm = get_plugin_manager()
    runs = _make_runs([7.0, 8.0, 9.0])
    results = _flatten(pm.hook.compute_evidence(campaign=campaign, runs=runs))
    kinds = {r.kind for r in results}
    assert kinds == {
        "statistical_summary",
        "lineage",
        "compute_resources",
        "code_fingerprint",
        "hardware_fingerprint",
    }


def test_stats_n0_returns_empty_list(campaign):
    pm = get_plugin_manager()
    results = _flatten(pm.hook.compute_evidence(campaign=campaign, runs=[]))
    assert not any(r.kind == "statistical_summary" for r in results)


def test_stats_basic_shape(campaign):
    """Statistical summary on 3 runs with known scores."""
    from evidence.builtin.stats import compute_evidence

    runs = _make_runs([5.0, 7.0, 9.0])
    [result] = compute_evidence(campaign=campaign, runs=runs)
    out = result.output
    assert out["n"] == 3
    assert out["mean"] == pytest.approx(7.0)
    assert out["min"] == 5.0
    assert out["max"] == 9.0
    assert out["best_run_id"] == "r3"
    assert result.deterministic is True


def test_stats_n1_collapses_ci(campaign):
    from evidence.builtin.stats import compute_evidence

    runs = _make_runs([7.5])
    [result] = compute_evidence(campaign=campaign, runs=runs)
    out = result.output
    assert out["sd"] == 0.0
    assert out["ci95_lower"] == out["ci95_upper"] == 7.5


def test_compute_resources_aggregates_timing(campaign):
    from evidence.builtin.compute import compute_evidence

    runs = _make_runs([7.0, 8.0])
    [result] = compute_evidence(campaign=campaign, runs=runs)
    out = result.output
    assert out["n_runs"] == 2
    assert out["total_wall_clock_seconds"] == pytest.approx(60.0)
    assert out["llm_call_count"] == 0
    assert out["code_execution_count"] == 0
    assert result.deterministic is True


def test_lineage_emits_empty_shape(campaign):
    from evidence.builtin.lineage import compute_evidence

    [result] = compute_evidence(campaign=campaign, runs=[])
    assert result.kind == "lineage"
    assert result.output == {
        "parent_bundle_ids": [],
        "child_bundle_ids": [],
        "external_refs": [],
    }


def test_code_fingerprint_runs_without_crashing(campaign):
    """Subprocess failures shouldn't crash; null fields are OK."""
    from evidence.builtin.code_fingerprint import compute_evidence

    [result] = compute_evidence(campaign=campaign, runs=[])
    assert result.kind == "code_fingerprint"
    assert result.deterministic is False
    # Either the keys are populated or None — both acceptable.
    assert "branch" in result.output
    assert "dirty_files" in result.output


def test_hardware_runs_without_crashing(campaign):
    from evidence.builtin.hardware import compute_evidence

    [result] = compute_evidence(campaign=campaign, runs=[])
    assert result.kind == "hardware_fingerprint"
    assert result.deterministic is False
    assert "hostname" in result.output
    assert isinstance(result.output["cpu_flags"], list)


def test_calculators_each_carry_independent_schema_versions(campaign):
    pm = get_plugin_manager()
    runs = _make_runs([7.0, 8.0])
    results = _flatten(pm.hook.compute_evidence(campaign=campaign, runs=runs))
    for r in results:
        assert r.schema_version
        assert r.calculator_id
