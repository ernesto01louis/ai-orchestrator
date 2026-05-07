"""Tests for core.db_writethrough — Phase 2.1 dual-write chokepoint.

The mocked layer (default suite) verifies:
* Disabled is a true no-op (no session opened).
* The conversion helpers map JSON shapes to DAO inputs correctly.
* Failures from the DAO call are swallowed (never raise).
* Hot-path scoping: ``mirror_campaigns`` only upserts the changed_ids,
  not the whole map.

Real round-trips live in 2.1.12 ``postgres_real`` integration tests.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from core import db, db_writethrough

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def disabled_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "is_enabled", lambda: False)


@pytest.fixture
def enabled_db_capturing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Patch core.db so:
    * is_enabled() returns True
    * get_session() yields a MagicMock session
    Returns a dict with the captured session for assertions."""
    captured: dict[str, Any] = {"session": MagicMock(name="MockSession")}
    monkeypatch.setattr(db, "is_enabled", lambda: True)

    from contextlib import contextmanager

    @contextmanager
    def _fake_session() -> Any:
        yield captured["session"]

    monkeypatch.setattr(db, "get_session", _fake_session)
    return captured


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def test_run_snapshot_to_row_maps_required_fields() -> None:
    row = db_writethrough._run_snapshot_to_row(
        "run-1",
        {
            "phase": "completed",
            "score": 9.5,
            "project": "demo",
            "target": "local",
            "error": None,
            "manifest_status": "ok",
        },
    )
    assert row["run_id"] == "run-1"
    assert row["project"] == "demo"
    assert row["target"] == "local"
    assert row["phase"] == "completed"
    assert row["score"] == 9.5
    assert row["completed"] is True
    assert row["has_error"] is False
    assert row["error_msg"] is None
    assert row["manifest_status"] == "ok"
    assert isinstance(row["completed_at"], datetime)
    assert row["completed_at"].tzinfo is not None


def test_run_snapshot_to_row_truncates_long_error() -> None:
    huge_error = "x" * 500
    row = db_writethrough._run_snapshot_to_row(
        "run-2",
        {"phase": "failed", "error": huge_error},
    )
    assert row["has_error"] is True
    assert row["error_msg"] is not None
    assert len(row["error_msg"]) == 200


def test_campaign_record_to_row_uses_dict_key_as_id() -> None:
    row = db_writethrough._campaign_record_to_row(
        "key-from-dict",
        {
            "id": "id-from-record",  # ignored — dict key wins
            "name": "demo",
            "status": "running",
            "hypothesis": "h",
            "template": {"k": "v"},
            "params": {"a": 1},
            "max_runs": 3,
            "parallelism": 2,
            "created_at": "2026-05-07T10:00:00Z",
            "completed_at": None,
        },
    )
    assert row["campaign_id"] == "key-from-dict"
    assert row["name"] == "demo"
    assert row["status"] == "running"
    assert row["template"] == {"k": "v"}
    assert row["params"] == {"a": 1}
    assert row["max_runs"] == 3
    assert row["parallelism"] == 2
    assert row["completed_at"] is None
    assert row["merkle_root"] is None
    assert row["merkle_status"] is None
    # Created_at must parse to a tz-aware datetime
    assert isinstance(row["created_at"], datetime)
    assert row["created_at"].tzinfo is not None


def test_campaign_record_to_row_handles_missing_optional_fields() -> None:
    row = db_writethrough._campaign_record_to_row(
        "minimal",
        {
            "name": "x",
            "status": "queued",
        },
    )
    assert row["campaign_id"] == "minimal"
    assert row["template"] == {}
    assert row["params"] == {}
    assert row["parallelism"] == 1
    assert row["completed_at"] is None
    assert row["created_at"].tzinfo is not None


def test_parse_iso_handles_z_suffix() -> None:
    parsed = db_writethrough._parse_iso("2026-05-07T10:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_iso_returns_none_for_falsy_or_garbage() -> None:
    assert db_writethrough._parse_iso(None) is None
    assert db_writethrough._parse_iso("") is None
    assert db_writethrough._parse_iso("not-a-date") is None


# ---------------------------------------------------------------------------
# Disabled mode — no session opened
# ---------------------------------------------------------------------------

def test_mirror_run_completion_no_op_when_disabled(
    disabled_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_session must NEVER be called when postgres is disabled."""
    spy = MagicMock(side_effect=AssertionError("get_session called when disabled"))
    monkeypatch.setattr(db, "get_session", spy)
    db_writethrough.mirror_run_completion("run-1", {"phase": "completed"})
    spy.assert_not_called()


def test_mirror_campaigns_no_op_when_disabled(
    disabled_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = MagicMock(side_effect=AssertionError("get_session called when disabled"))
    monkeypatch.setattr(db, "get_session", spy)
    db_writethrough.mirror_campaigns(
        {"camp-1": {"name": "x", "status": "running"}},
        changed_ids={"camp-1"},
    )
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# Enabled mode — DAOs called with mapped data
# ---------------------------------------------------------------------------

def test_mirror_run_completion_calls_upsert_run(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upsert_spy = MagicMock()
    monkeypatch.setattr("core.db_models.upsert_run", upsert_spy)
    db_writethrough.mirror_run_completion(
        "run-7",
        {"phase": "completed", "score": 8.0, "project": "p", "target": "t"},
    )
    upsert_spy.assert_called_once()
    session_arg, row = upsert_spy.call_args.args
    assert session_arg is enabled_db_capturing_session["session"]
    assert row["run_id"] == "run-7"
    assert row["score"] == 8.0


def test_mirror_run_completion_swallows_db_exception(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "core.db_models.upsert_run",
        MagicMock(side_effect=RuntimeError("postgres down")),
    )
    # Must not raise
    db_writethrough.mirror_run_completion("run-x", {"phase": "completed"})
    assert any(
        "postgres_writethrough_failed" in r.message for r in caplog.records
    ), f"expected WARN log; got {[r.message for r in caplog.records]}"


def test_mirror_campaigns_scopes_to_changed_ids(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upsert_spy = MagicMock()
    monkeypatch.setattr("core.db_models.upsert_campaign", upsert_spy)
    full_map = {
        "camp-1": {"name": "a", "status": "running"},
        "camp-2": {"name": "b", "status": "running"},
        "camp-3": {"name": "c", "status": "queued"},
    }
    db_writethrough.mirror_campaigns(full_map, changed_ids={"camp-2"})
    assert upsert_spy.call_count == 1
    _, row = upsert_spy.call_args.args
    assert row["campaign_id"] == "camp-2"
    assert row["name"] == "b"


def test_mirror_campaigns_full_sweep_when_changed_ids_is_none(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upsert_spy = MagicMock()
    monkeypatch.setattr("core.db_models.upsert_campaign", upsert_spy)
    db_writethrough.mirror_campaigns(
        {
            "c1": {"name": "x", "status": "running"},
            "c2": {"name": "y", "status": "queued"},
        },
        changed_ids=None,
    )
    assert upsert_spy.call_count == 2


def test_mirror_campaigns_skips_unknown_changed_id(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upsert_spy = MagicMock()
    monkeypatch.setattr("core.db_models.upsert_campaign", upsert_spy)
    db_writethrough.mirror_campaigns(
        {"camp-1": {"name": "x", "status": "running"}},
        changed_ids={"camp-1", "ghost-id"},
    )
    assert upsert_spy.call_count == 1


def test_mirror_campaigns_swallows_db_exception(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "core.db_models.upsert_campaign",
        MagicMock(side_effect=RuntimeError("oh no")),
    )
    db_writethrough.mirror_campaigns(
        {"camp-1": {"name": "x", "status": "running"}},
        changed_ids={"camp-1"},
    )
    # Did not raise
    assert any(
        "postgres_writethrough_failed" in r.message for r in caplog.records
    )


def test_mirror_campaigns_empty_changed_ids_skips_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If changed_ids is empty (caller knows nothing changed), don't even
    open a session."""
    monkeypatch.setattr(db, "is_enabled", lambda: True)
    spy = MagicMock(side_effect=AssertionError("get_session called for empty set"))
    monkeypatch.setattr(db, "get_session", spy)
    db_writethrough.mirror_campaigns({}, changed_ids=set())
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# mirror_llm_call (2.1.8)
# ---------------------------------------------------------------------------

def _make_llm_record(**overrides: Any) -> Any:
    """Construct a real LlmCallRecord via the dataclass constructor.

    Importing here so the test module doesn't import core.llm_call_log
    at collection time.
    """
    from core.llm_call_log import LlmCallRecord
    defaults = {
        "run_id": "run-1",
        "model": "qwen2.5:72b",
        "rendered_messages": [{"role": "user", "content": "hi"}],
        "sampling": {"temperature": 0.0},
        "response_tokens": 42,
        "duration_ms": 1234,
        "call_id": "task-uuid-1",
        "agent_role": "generator",
        "server_url": "http://192.168.2.10:11434",
        "model_digest": "sha256:abc",
        "model_size_bytes": 1234567890,
        "response_text": "hello",
    }
    defaults.update(overrides)
    return LlmCallRecord(**defaults)


def test_llm_call_record_to_row_renames_fields() -> None:
    record = _make_llm_record()
    row = db_writethrough._llm_call_record_to_row(record)
    # Field renames: model → model_name, server_url → host
    assert row["model_name"] == "qwen2.5:72b"
    assert row["host"] == "http://192.168.2.10:11434"
    # Rest of the columns
    assert row["call_id"] == "task-uuid-1"
    assert row["run_id"] == "run-1"
    assert row["agent_role"] == "generator"
    assert row["duration_ms"] == 1234
    assert row["response_tokens"] == 42
    assert row["sampling"] == {"temperature": 0.0}
    assert row["response_text"] == "hello"
    assert row["model_size_bytes"] == 1234567890


def test_mirror_llm_call_no_op_when_disabled(
    disabled_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = MagicMock(side_effect=AssertionError("get_session called when disabled"))
    monkeypatch.setattr(db, "get_session", spy)
    db_writethrough.mirror_llm_call(_make_llm_record())
    spy.assert_not_called()


def test_mirror_llm_call_skips_record_with_empty_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty call_id would collide on the PK and get DO NOTHING'd —
    skip the postgres write entirely."""
    monkeypatch.setattr(db, "is_enabled", lambda: True)
    spy = MagicMock(side_effect=AssertionError("get_session called with empty call_id"))
    monkeypatch.setattr(db, "get_session", spy)
    db_writethrough.mirror_llm_call(_make_llm_record(call_id=""))
    spy.assert_not_called()


def test_mirror_llm_call_calls_insert_llm_call(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = MagicMock()
    monkeypatch.setattr("core.db_models.insert_llm_call", spy)
    db_writethrough.mirror_llm_call(_make_llm_record())
    spy.assert_called_once()
    session_arg, row = spy.call_args.args
    assert session_arg is enabled_db_capturing_session["session"]
    assert row["call_id"] == "task-uuid-1"
    assert row["model_name"] == "qwen2.5:72b"


def test_mirror_llm_call_swallows_db_exception(
    enabled_db_capturing_session: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "core.db_models.insert_llm_call",
        MagicMock(side_effect=RuntimeError("postgres unreachable")),
    )
    db_writethrough.mirror_llm_call(_make_llm_record())
    assert any(
        "postgres_writethrough_failed" in r.message for r in caplog.records
    )
