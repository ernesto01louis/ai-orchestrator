"""Tests for core.db_reconcile — Phase 2.1 reconcile-on-startup."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core import db, db_reconcile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_canonical_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Path]:
    """Lay out the JSON canonical files under tmp_path and patch the
    paths module to point reconcile at them."""
    run_index = tmp_path / "run_index.json"
    campaigns_file = tmp_path / "campaigns.json"
    model_stats = tmp_path / "model_stats.json"
    campaigns_dir = tmp_path / "campaigns_dir"
    campaigns_dir.mkdir()

    monkeypatch.setattr(db_reconcile, "RUN_INDEX_FILE", run_index)
    monkeypatch.setattr(db_reconcile, "CAMPAIGNS_FILE", campaigns_file)
    monkeypatch.setattr(db_reconcile, "MODEL_STATS", model_stats)
    monkeypatch.setattr(
        db_reconcile,
        "_resolve_campaigns_dir",
        lambda: campaigns_dir,
    )

    return {
        "run_index": run_index,
        "campaigns_file": campaigns_file,
        "model_stats": model_stats,
        "campaigns_dir": campaigns_dir,
    }


@pytest.fixture
def enabled_db_capturing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    captured: dict[str, Any] = {"session": MagicMock(name="MockSession")}
    monkeypatch.setattr(db, "is_enabled", lambda: True)

    from contextlib import contextmanager

    @contextmanager
    def _fake_session() -> Any:
        yield captured["session"]

    monkeypatch.setattr(db, "get_session", _fake_session)
    return captured


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------

def test_reconcile_all_no_op_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "is_enabled", lambda: False)
    spy = MagicMock(side_effect=AssertionError("get_session called when disabled"))
    monkeypatch.setattr(db, "get_session", spy)
    result = db_reconcile.reconcile_all()
    assert result == {"skipped": True}
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# Per-table reconcile counts
# ---------------------------------------------------------------------------

def test_reconcile_runs_imports_run_index(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_canonical_state["run_index"].write_text(
        json.dumps({
            "run-1": {
                "phase": "completed",
                "score": 9.0,
                "project": "demo",
                "target": "local",
                "has_error": False,
                "error_msg": None,
                "timestamp": "2026-05-01T10:00:00Z",
            },
            "run-2": {
                "phase": "failed",
                "score": 0,
                "project": "demo",
                "target": "local",
                "has_error": True,
                "error_msg": "boom",
                "timestamp": "2026-05-02T10:00:00Z",
            },
        })
    )
    upsert_run_spy = MagicMock()
    monkeypatch.setattr("core.db_models.upsert_run", upsert_run_spy)
    # Stub the rest so they don't fire
    monkeypatch.setattr("core.db_models.upsert_campaign", MagicMock())
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["runs"] == 2
    assert upsert_run_spy.call_count == 2
    # First call: run-1
    first_row = upsert_run_spy.call_args_list[0].args[1]
    assert first_row["run_id"] == "run-1"
    assert first_row["score"] == 9.0
    # Timestamp from JSON wins, not "now"
    assert first_row["completed_at"].year == 2026
    assert first_row["completed_at"].month == 5


def test_reconcile_campaigns_imports_full_map(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_canonical_state["campaigns_file"].write_text(
        json.dumps({
            "camp-1": {
                "name": "demo",
                "status": "completed",
                "hypothesis": "h",
                "template": {},
                "params": {},
                "created_at": "2026-05-01T10:00:00Z",
            },
        })
    )
    upsert_campaign_spy = MagicMock()
    monkeypatch.setattr("core.db_models.upsert_campaign", upsert_campaign_spy)
    monkeypatch.setattr("core.db_models.upsert_run", MagicMock())
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["campaigns"] == 1
    upsert_campaign_spy.assert_called_once()
    row = upsert_campaign_spy.call_args.args[1]
    assert row["campaign_id"] == "camp-1"


def test_reconcile_evidence_bundles_walks_campaigns_dir(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crate_dir = tmp_canonical_state["campaigns_dir"] / "camp-1"
    crate_dir.mkdir()
    (crate_dir / "manifest.json.dsse").write_bytes(b'{"signed":true}')
    (crate_dir / "evidence.json").write_text(
        json.dumps({
            "schema_version": "1.0.0",
            "bundle_id": "01HXY-bundle",
            "campaign_id": "camp-1",
            "created_at": "2026-05-01T10:00:00Z",
        })
    )
    insert_spy = MagicMock()
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", insert_spy)
    monkeypatch.setattr("core.db_models.upsert_run", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_campaign", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["evidence_bundles"] == 1
    insert_spy.assert_called_once()
    row = insert_spy.call_args.args[1]
    assert row["bundle_id"] == "01HXY-bundle"
    assert row["campaign_id"] == "camp-1"
    assert str(row["crate_path"]).endswith("camp-1")
    # crate_sha256 = sha256 of the envelope file (real sha256 of `{"signed":true}`)
    assert row["crate_sha256"] != ""
    assert len(row["crate_sha256"]) == 64


def test_reconcile_evidence_skips_dirs_without_envelope(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A campaign dir without manifest.json.dsse is skipped silently."""
    crate_dir = tmp_canonical_state["campaigns_dir"] / "no-bundle"
    crate_dir.mkdir()
    (crate_dir / "evidence.json").write_text("{}")  # no envelope
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_run", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_campaign", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["evidence_bundles"] == 0


def test_reconcile_model_stats_seed_rows_marked_correctly(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_canonical_state["model_stats"].write_text(
        json.dumps({
            "qwen2.5:72b": {
                "total_runs": 42,
                "total_score": 320.5,
                "wins": 19,
                "failures": 3,
                "by_language": {"python": {"runs": 30}},
                "by_role": {"generator": {"runs": 30}},
                "by_project_type": {"script": {"runs": 30}},
            },
            "deepseek-coder": {
                "total_runs": 10,
                "total_score": 70.0,
                "wins": 4,
                "failures": 1,
            },
        })
    )
    seed_spy = MagicMock()
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", seed_spy)
    monkeypatch.setattr("core.db_models.upsert_run", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_campaign", MagicMock())
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["model_stats_seed_rows"] == 2
    assert seed_spy.call_count == 2
    rows = [c.args[1] for c in seed_spy.call_args_list]
    for row in rows:
        assert row["source"] == "reconcile_seed"
    qwen_row = next(r for r in rows if r["model_name"] == "qwen2.5:72b")
    assert qwen_row["runs"] == 42
    assert qwen_row["wins"] == 19
    assert qwen_row["by_language"] == {"python": {"runs": 30}}


def test_reconcile_all_returns_full_count_dict(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_canonical_state["run_index"].write_text("{}")
    tmp_canonical_state["campaigns_file"].write_text("{}")
    tmp_canonical_state["model_stats"].write_text("{}")
    monkeypatch.setattr("core.db_models.upsert_run", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_campaign", MagicMock())
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["runs"] == 0
    assert counts["campaigns"] == 0
    assert counts["evidence_bundles"] == 0
    assert counts["model_stats_seed_rows"] == 0
    assert "duration_ms" in counts


def test_reconcile_tolerates_missing_json_files(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a JSON file doesn't exist, reconcile must continue with the
    others and report 0 for the missing one."""
    # Don't write any files — tmp_path is empty
    monkeypatch.setattr("core.db_models.upsert_run", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_campaign", MagicMock())
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["runs"] == 0
    assert counts["campaigns"] == 0


def test_reconcile_tolerates_corrupt_json(
    enabled_db_capturing_session: dict[str, Any],
    tmp_canonical_state: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tmp_canonical_state["run_index"].write_text("not valid json {")
    monkeypatch.setattr("core.db_models.upsert_run", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_campaign", MagicMock())
    monkeypatch.setattr("core.db_models.insert_evidence_bundle", MagicMock())
    monkeypatch.setattr("core.db_models.upsert_model_stats_daily", MagicMock())

    counts = db_reconcile.reconcile_all()
    assert counts["runs"] == 0
    assert any(
        "reconcile_json_unreadable" in r.message for r in caplog.records
    )
