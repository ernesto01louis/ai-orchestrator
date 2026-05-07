"""Tests for core.db_models — ORM mappings + DAO functions (Phase 2.1)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from core.db_models import (
    Base,
    Campaign,
    EvidenceBundle,
    LlmCall,
    ModelStatsDaily,
    Run,
    insert_evidence_bundle,
    insert_llm_call,
    upsert_campaign,
    upsert_model_stats_daily,
    upsert_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _captured_stmt(session: MagicMock) -> str:
    """Return the SQL of the last stmt passed to session.execute().

    Compiles against the postgresql dialect (parameter placeholders
    instead of literals — JSONB doesn't support literal-binds rendering,
    and we only assert SQL keywords and column names anyway).
    """
    assert session.execute.call_count == 1, (
        f"expected one execute() call; got {session.execute.call_count}"
    )
    stmt = session.execute.call_args.args[0]
    return str(stmt.compile(dialect=postgresql.dialect()))


# ---------------------------------------------------------------------------
# ORM smoke
# ---------------------------------------------------------------------------


def test_base_metadata_has_all_five_tables() -> None:
    table_names = set(Base.metadata.tables.keys())
    for expected in (
        "campaigns",
        "runs",
        "llm_calls",
        "evidence_bundles",
        "model_stats_daily",
    ):
        assert expected in table_names


def test_campaign_orm_columns() -> None:
    cols = {c.name for c in Campaign.__table__.columns}
    assert "campaign_id" in cols
    assert "merkle_root" in cols
    assert "merkle_status" in cols


def test_run_orm_has_phase_15_attestation_columns() -> None:
    cols = {c.name for c in Run.__table__.columns}
    assert "manifest_sha256" in cols
    assert "manifest_status" in cols


def test_llm_call_orm_columns() -> None:
    cols = {c.name for c in LlmCall.__table__.columns}
    for expected in (
        "call_id",
        "run_id",
        "agent_role",
        "model_name",
        "model_digest",
        "duration_ms",
        "response_text",
    ):
        assert expected in cols


def test_evidence_bundle_orm_columns() -> None:
    cols = {c.name for c in EvidenceBundle.__table__.columns}
    assert "crate_sha256" in cols
    assert "crate_path" in cols


def test_model_stats_daily_composite_pk() -> None:
    pk = {c.name for c in ModelStatsDaily.__table__.primary_key.columns}
    assert pk == {"model_name", "date"}


# ---------------------------------------------------------------------------
# upsert_run
# ---------------------------------------------------------------------------


def test_upsert_run_emits_on_conflict_do_update() -> None:
    session = MagicMock()
    upsert_run(
        session,
        {
            "run_id": "abc-123",
            "project": "demo",
            "target": "local",
            "phase": "completed",
            "score": 9.5,
            "completed": True,
            "completed_at": datetime(2026, 5, 7, 10, 0, tzinfo=UTC),
        },
    )
    sql = _captured_stmt(session)
    assert "INSERT INTO runs" in sql
    assert "ON CONFLICT (run_id) DO UPDATE" in sql
    # PK is excluded from the SET clause (otherwise we'd UPDATE the conflict key)
    assert "run_id = excluded.run_id" not in sql.lower()


def test_upsert_run_requires_run_id() -> None:
    session = MagicMock()
    with pytest.raises(ValueError, match="run_id"):
        upsert_run(session, {"project": "x", "completed_at": datetime.now(UTC)})


def test_upsert_run_requires_completed_at() -> None:
    session = MagicMock()
    with pytest.raises(ValueError, match="completed_at"):
        upsert_run(session, {"run_id": "x"})


# ---------------------------------------------------------------------------
# upsert_campaign
# ---------------------------------------------------------------------------


def test_upsert_campaign_emits_on_conflict_do_update() -> None:
    session = MagicMock()
    upsert_campaign(
        session,
        {
            "campaign_id": "camp-1",
            "name": "demo",
            "status": "running",
            "hypothesis": "h",
            "template": {"k": "v"},
            "params": {"a": 1},
            "created_at": datetime(2026, 5, 7, tzinfo=UTC),
        },
    )
    sql = _captured_stmt(session)
    assert "INSERT INTO campaigns" in sql
    assert "ON CONFLICT (campaign_id) DO UPDATE" in sql


def test_upsert_campaign_requires_required_fields() -> None:
    session = MagicMock()
    with pytest.raises(ValueError, match="name"):
        upsert_campaign(
            session,
            {"campaign_id": "c", "status": "running", "created_at": datetime.now(UTC)},
        )


# ---------------------------------------------------------------------------
# insert_llm_call
# ---------------------------------------------------------------------------


def test_insert_llm_call_emits_on_conflict_do_nothing() -> None:
    session = MagicMock()
    insert_llm_call(
        session,
        {
            "call_id": "task-uuid-1",
            "run_id": "run-1",
            "agent_role": "generator",
            "model_name": "qwen2.5:72b",
            "duration_ms": 1234,
        },
    )
    sql = _captured_stmt(session)
    assert "INSERT INTO llm_calls" in sql
    assert "ON CONFLICT (call_id) DO NOTHING" in sql


def test_insert_llm_call_requires_call_id_and_run_id() -> None:
    session = MagicMock()
    with pytest.raises(ValueError, match="call_id|run_id"):
        insert_llm_call(session, {"agent_role": "judge"})


# ---------------------------------------------------------------------------
# insert_evidence_bundle
# ---------------------------------------------------------------------------


def test_insert_evidence_bundle_emits_on_conflict_do_nothing() -> None:
    session = MagicMock()
    insert_evidence_bundle(
        session,
        {
            "bundle_id": "bundle-ulid-1",
            "campaign_id": "camp-1",
            "crate_path": "/opt/ai-orchestrator/campaigns/camp-1",
            "crate_sha256": "deadbeef",
            "created_at": datetime.now(UTC),
        },
    )
    sql = _captured_stmt(session)
    assert "INSERT INTO evidence_bundles" in sql
    assert "ON CONFLICT (bundle_id) DO NOTHING" in sql


def test_insert_evidence_bundle_requires_required_fields() -> None:
    session = MagicMock()
    with pytest.raises(ValueError, match="missing fields"):
        insert_evidence_bundle(session, {"bundle_id": "b"})


# ---------------------------------------------------------------------------
# upsert_model_stats_daily — atomic increment is the load-bearing test
# ---------------------------------------------------------------------------


def test_upsert_model_stats_daily_uses_atomic_increment() -> None:
    session = MagicMock()
    upsert_model_stats_daily(
        session,
        {
            "model_name": "qwen2.5:72b",
            "date": date(2026, 5, 7),
            "runs": 1,
            "total_score": 9.0,
            "wins": 1,
            "failures": 0,
            "by_language": {"python": {"runs": 1, "wins": 1}},
            "by_role": {"generator": {"runs": 1, "wins": 1}},
            "by_project_type": {"script": {"runs": 1, "wins": 1}},
            "updated_at": datetime(2026, 5, 7, 10, 0, tzinfo=UTC),
        },
    )
    sql = _captured_stmt(session)
    assert "INSERT INTO model_stats_daily" in sql
    assert "ON CONFLICT (model_name, date) DO UPDATE" in sql
    # The load-bearing claim: counters use server-side increment,
    # not client-side read-modify-write.
    sql_lower = sql.lower()
    assert "model_stats_daily.runs + excluded.runs" in sql_lower
    assert "model_stats_daily.wins + excluded.wins" in sql_lower
    assert "model_stats_daily.failures + excluded.failures" in sql_lower
    assert "model_stats_daily.total_score + excluded.total_score" in sql_lower
    # Source must NOT be in the SET clause — once 'live', stays 'live'.
    set_clause = sql_lower.split("do update set", 1)[1]
    assert "source" not in set_clause


def test_upsert_model_stats_daily_defaults_zero_counters() -> None:
    session = MagicMock()
    # Only the bare-minimum required fields — counters default to 0.
    upsert_model_stats_daily(
        session,
        {
            "model_name": "qwen2.5:72b",
            "date": date(2026, 5, 7),
            "updated_at": datetime(2026, 5, 7, 10, 0, tzinfo=UTC),
        },
    )
    sql = _captured_stmt(session)
    assert "INSERT INTO model_stats_daily" in sql


def test_upsert_model_stats_daily_requires_required_fields() -> None:
    session = MagicMock()
    with pytest.raises(ValueError, match="model_name|date|updated_at"):
        upsert_model_stats_daily(session, {})
    with pytest.raises(ValueError, match="updated_at"):
        upsert_model_stats_daily(
            session, {"model_name": "m", "date": date(2026, 5, 7)}
        )
