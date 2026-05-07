"""Phase 2.1 — Postgres durable system-of-record (engine + sessions).

JSON files under ``memory/`` / ``runs/`` / ``campaigns/`` remain canonical;
Postgres is the queryable mirror that enables Phase 2.4 budget aggregates
and Phase 2.6 UI list/filter/sort. Dual-writes are JSON-first,
Postgres-second and sit behind ``core.db_writethrough`` (added in 2.1.7).

This module is the engine + session factory. It is intentionally tiny:

* ``is_enabled()`` — single source of truth for "should we attempt
  Postgres writes?". Returns ``False`` when ``postgres.enabled=false`` in
  config.json or when the resolved DSN is empty. Every dual-write
  callsite checks this first.
* ``get_engine()`` — lazily constructs and caches the SQLAlchemy 2.0
  engine. Pool size, max overflow come from config. ``pool_pre_ping=True``
  protects against stale connections after Postgres restarts.
* ``get_session()`` — context manager. Opens a transaction, applies
  ``SET LOCAL statement_timeout`` so an LXC-overload incident can't stall
  Prefect tasks, commits on success, rolls back on exception, always
  closes. Raises ``RuntimeError`` if called when Postgres is disabled —
  callers MUST gate on ``is_enabled()`` first (the writethrough wrapper
  does this once).
* ``reset_for_tests()`` — drops the cached engine + session factory so
  tests can re-initialize against different config.

Driver: ``postgresql+psycopg://`` (psycopg 3.x). Sync only — async
SQLAlchemy would propagate ``await`` through ~3000 LOC of orchestration
code for no real win, since every dual-write callsite is already inside
a Prefect ``@task`` body or a sync FastAPI handler.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core import config

# ---------------------------------------------------------------------------
# Module-level singletons (lazy-initialized, thread-safe)
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
# RLock so get_session_factory() can call get_engine() while holding the
# init lock without self-deadlocking.
_init_lock = threading.RLock()


def is_enabled() -> bool:
    """Return whether Postgres dual-write is wired up.

    True only when both ``postgres.enabled=true`` in config.json AND a
    non-empty DSN is resolvable (env var ``POSTGRES_DSN`` wins over the
    config.json value).
    """
    return bool(config.POSTGRES_ENABLED and config.POSTGRES_DSN)


def _resolve_sqlalchemy_url() -> str:
    """Coerce ``POSTGRES_DSN`` to the SQLAlchemy ``postgresql+psycopg`` form.

    Operators write ``postgresql://`` in the DSN (standard libpq form);
    SQLAlchemy needs the ``+psycopg`` suffix to pick the v3 driver instead
    of psycopg2 (which we don't ship).
    """
    raw = config.POSTGRES_DSN
    if raw.startswith("postgresql+"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://"):]
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://"):]
    return raw


def get_engine() -> Engine:
    """Return the cached SQLAlchemy engine, building it on first call.

    Raises ``RuntimeError`` if Postgres is not enabled — callers must
    check :func:`is_enabled` first.
    """
    global _engine
    if _engine is not None:
        return _engine

    if not is_enabled():
        raise RuntimeError(
            "core.db.get_engine() called with Postgres disabled — "
            "check is_enabled() before requesting an engine."
        )

    with _init_lock:
        if _engine is not None:  # double-checked locking
            return _engine
        _engine = sa.create_engine(
            _resolve_sqlalchemy_url(),
            pool_size=config.POSTGRES_POOL_SIZE,
            max_overflow=config.POSTGRES_POOL_MAX_OVERFLOW,
            pool_pre_ping=True,
            future=True,
        )
        return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the cached sessionmaker, building it on first call."""
    global _session_factory
    if _session_factory is not None:
        return _session_factory

    with _init_lock:
        if _session_factory is not None:
            return _session_factory
        _session_factory = sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            future=True,
        )
        return _session_factory


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a Session inside a transaction with statement_timeout applied.

    Callers MUST check :func:`is_enabled` first; calling this when
    Postgres is disabled raises ``RuntimeError`` (loud failure beats a
    silent no-op when a writethrough callsite is misconfigured).

    On exit:
    * Normal — commits the transaction, closes the session.
    * Exception — rolls back, closes the session, re-raises.

    ``SET LOCAL`` only takes effect inside a transaction. SQLAlchemy 2.0
    sessions begin transactions lazily; the SET is the first statement
    executed, so it sticks for the rest of the block.
    """
    if not is_enabled():
        raise RuntimeError(
            "core.db.get_session() called with Postgres disabled — "
            "check is_enabled() before opening a session."
        )

    factory = get_session_factory()
    session = factory()
    try:
        timeout_ms = max(100, config.POSTGRES_STATEMENT_TIMEOUT_MS)
        session.execute(sa.text(f"SET LOCAL statement_timeout = {timeout_ms}"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_for_tests() -> None:
    """Drop cached engine + session factory.

    Tests that exercise different config values (e.g. monkeypatching
    ``config.POSTGRES_ENABLED``) must call this first, otherwise the
    cached singletons keep the previous config alive.
    """
    global _engine, _session_factory
    with _init_lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:  # pragma: no cover — defensive
                pass
        _engine = None
        _session_factory = None
