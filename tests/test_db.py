"""Tests for core.db — Postgres engine + session factory (Phase 2.1).

Two layers:

* **Mocked / disabled** (default suite) — verifies that:
    - is_enabled() reflects config flags
    - get_engine() / get_session() raise loudly when disabled
    - Engine is built lazily and cached
    - DSN coercion adds the +psycopg driver suffix

* **postgres_real** marker — runs only when CI or an operator points
  $POSTGRES_DSN at a live Postgres. Verifies a real round-trip and that
  statement_timeout is actually applied inside the session.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from core import config, db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_db_singletons() -> Iterator[None]:
    """Each test starts with no cached engine — different tests use
    different config values."""
    db.reset_for_tests()
    yield
    db.reset_for_tests()


@pytest.fixture
def disabled_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "POSTGRES_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "POSTGRES_DSN", "", raising=False)


@pytest.fixture
def enabled_mocked_config(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Postgres reports as enabled with a fake DSN; ``sa.create_engine``
    is patched so the cache/singleton tests don't try to actually
    connect anywhere. Real connection behavior lives under the
    ``postgres_real`` marker.
    """
    from unittest.mock import MagicMock

    monkeypatch.setattr(config, "POSTGRES_ENABLED", True, raising=False)
    monkeypatch.setattr(
        config,
        "POSTGRES_DSN",
        "postgresql://x:y@host/db",
        raising=False,
    )
    monkeypatch.setattr(config, "POSTGRES_POOL_SIZE", 5, raising=False)
    monkeypatch.setattr(config, "POSTGRES_POOL_MAX_OVERFLOW", 5, raising=False)
    monkeypatch.setattr(config, "POSTGRES_STATEMENT_TIMEOUT_MS", 5000, raising=False)

    # Each call returns a fresh MagicMock so identity comparisons in
    # tests still validate the cache (we want to see the SAME mock back
    # on the second get_engine() call, which means we built it once).
    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        return MagicMock(name="MockEngine")

    monkeypatch.setattr("core.db.sa.create_engine", _factory)
    yield


# ---------------------------------------------------------------------------
# is_enabled()
# ---------------------------------------------------------------------------

def test_is_enabled_false_by_default(disabled_config: None) -> None:
    assert db.is_enabled() is False


def test_is_enabled_false_when_dsn_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "POSTGRES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "POSTGRES_DSN", "", raising=False)
    assert db.is_enabled() is False


def test_is_enabled_true_with_dsn_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "POSTGRES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "POSTGRES_DSN", "postgresql://x:y@host/db", raising=False)
    assert db.is_enabled() is True


# ---------------------------------------------------------------------------
# Disabled-mode safety: every entry point must raise loudly
# ---------------------------------------------------------------------------

def test_get_engine_raises_when_disabled(disabled_config: None) -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        db.get_engine()


def test_get_session_raises_when_disabled(disabled_config: None) -> None:
    # Context-manager protocol: the RuntimeError fires on __enter__
    with pytest.raises(RuntimeError, match="disabled"):
        with db.get_session():
            pass  # pragma: no cover — never reached


# ---------------------------------------------------------------------------
# DSN coercion
# ---------------------------------------------------------------------------

def test_resolve_sqlalchemy_url_adds_psycopg_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "POSTGRES_DSN",
        "postgresql://orchestrator:secret@192.168.2.183:5432/orchestrator",
        raising=False,
    )
    assert db._resolve_sqlalchemy_url() == (
        "postgresql+psycopg://orchestrator:secret@192.168.2.183:5432/orchestrator"
    )


def test_resolve_sqlalchemy_url_handles_postgres_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``postgres://`` (without the ``ql``) — common shorthand — must also
    get coerced."""
    monkeypatch.setattr(config, "POSTGRES_DSN", "postgres://x:y@h/db", raising=False)
    assert db._resolve_sqlalchemy_url() == "postgresql+psycopg://x:y@h/db"


def test_resolve_sqlalchemy_url_passes_through_explicit_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If an operator already wrote ``postgresql+psycopg://`` we leave it
    alone (lets them override the driver if they ever ship asyncpg)."""
    monkeypatch.setattr(
        config, "POSTGRES_DSN", "postgresql+psycopg://x:y@h/db", raising=False
    )
    assert db._resolve_sqlalchemy_url() == "postgresql+psycopg://x:y@h/db"


# ---------------------------------------------------------------------------
# Engine + session caching (uses SQLite to exercise SA plumbing)
# ---------------------------------------------------------------------------

def test_get_engine_caches_singleton(enabled_mocked_config: None) -> None:
    e1 = db.get_engine()
    e2 = db.get_engine()
    assert e1 is e2


def test_get_session_factory_caches_singleton(enabled_mocked_config: None) -> None:
    f1 = db.get_session_factory()
    f2 = db.get_session_factory()
    assert f1 is f2


def test_reset_for_tests_releases_singletons(enabled_mocked_config: None) -> None:
    e1 = db.get_engine()
    db.reset_for_tests()
    e2 = db.get_engine()
    assert e1 is not e2


# ---------------------------------------------------------------------------
# Real-server tests (gated by postgres_real marker)
# ---------------------------------------------------------------------------

@pytest.mark.postgres_real
def test_real_session_round_trips_select_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: real Postgres, real session, ``SELECT 1`` returns 1."""
    import sqlalchemy as sa

    dsn = os.getenv("POSTGRES_DSN", "")
    assert dsn, "postgres_real marker requires POSTGRES_DSN"
    monkeypatch.setattr(config, "POSTGRES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "POSTGRES_DSN", dsn, raising=False)

    with db.get_session() as session:
        result = session.execute(sa.text("SELECT 1")).scalar_one()
    assert result == 1


@pytest.mark.postgres_real
def test_real_session_applies_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the session, current_setting('statement_timeout') must
    reflect the configured millis (Postgres reports it as e.g. ``5s``,
    so we just assert it's not the default ``0``)."""
    import sqlalchemy as sa

    dsn = os.getenv("POSTGRES_DSN", "")
    assert dsn, "postgres_real marker requires POSTGRES_DSN"
    monkeypatch.setattr(config, "POSTGRES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "POSTGRES_DSN", dsn, raising=False)
    monkeypatch.setattr(config, "POSTGRES_STATEMENT_TIMEOUT_MS", 5000, raising=False)

    with db.get_session() as session:
        timeout: Any = session.execute(
            sa.text("SHOW statement_timeout")
        ).scalar_one()
    # Postgres reports "5s" / "5000ms" / similar — the key check is
    # "not zero", which is the per-cluster default we DON'T want.
    assert timeout not in ("0", "")
