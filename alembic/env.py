"""Alembic environment for the AI Orchestrator Phase 2.1 durable store.

We deliberately do NOT import ``core.config`` here. ``core.config`` is
loaded eagerly at import time and pulls in FastAPI / Pydantic / Prefect
surface area; this would couple migration runs (CI, fresh deploys) to
the rest of the application. Instead, we parse ``.env`` directly with
``python-dotenv`` and pull ``POSTGRES_DSN`` from there or the ambient
environment.

The DSN coercion (``postgresql://`` → ``postgresql+psycopg://``)
mirrors :func:`core.db._resolve_sqlalchemy_url`.
"""
from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context

# ---------------------------------------------------------------------------
# Load .env at the repo root so POSTGRES_DSN resolves from there.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# ---------------------------------------------------------------------------
# Standard Alembic plumbing
# ---------------------------------------------------------------------------

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Phase 2.1: the ORM models in core.db_models will register their
# metadata onto a single Base. We import lazily because 2.1.5 lands
# *before* 2.1.6 — until db_models.py exists, target_metadata is None.
target_metadata = None
try:
    from core.db_models import Base  # type: ignore[import-not-found]

    target_metadata = Base.metadata
except ImportError:
    # 2.1.5 first-shipping state: db_models.py not landed yet.
    target_metadata = None


def _resolve_dsn() -> str:
    """Return a SQLAlchemy URL with the +psycopg driver suffix.

    Operators set ``POSTGRES_DSN`` in libpq form (``postgresql://``);
    SQLAlchemy needs ``postgresql+psycopg://`` to pick the v3 driver.
    """
    dsn = os.getenv("POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "POSTGRES_DSN env var is empty — set it in .env (or the "
            "environment) before running alembic."
        )
    if dsn.startswith("postgresql+"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn[len("postgresql://"):]
    if dsn.startswith("postgres://"):
        return "postgresql+psycopg://" + dsn[len("postgres://"):]
    return dsn


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to the script output."""
    url = _resolve_dsn()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect and execute against the DB."""
    config.set_main_option("sqlalchemy.url", _resolve_dsn())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
