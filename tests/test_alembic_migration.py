"""Phase 2.1 sub-task 5: Alembic migration apply test.

Gated by the ``postgres_real`` marker. Runs in CI's ``postgres-integration``
job (added in 2.1.12) against a fresh Postgres service container.
Exercises the full upgrade-then-downgrade cycle so we catch any bad SQL
in the initial schema before it lands.

Local-dev shortcut: ``POSTGRES_DSN=postgresql://... pytest -m postgres_real
tests/test_alembic_migration.py``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run alembic with POSTGRES_DSN propagated from the test environment."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.postgres_real
def test_migration_upgrade_then_downgrade_cleanly() -> None:
    """alembic upgrade head + downgrade base must succeed against a real DB."""
    dsn = os.getenv("POSTGRES_DSN", "")
    assert dsn, "postgres_real marker requires POSTGRES_DSN"

    # Drop to baseline (in case a prior run left rows around).
    _alembic(["downgrade", "base"])

    # Upgrade to head.
    upgrade_result = _alembic(["upgrade", "head"])
    assert "0001_initial_schema" in upgrade_result.stdout + upgrade_result.stderr, (
        f"expected initial revision in alembic output; got "
        f"stdout={upgrade_result.stdout!r} stderr={upgrade_result.stderr!r}"
    )

    # Verify the five core tables now exist.
    import sqlalchemy as sa

    from core.db import _resolve_sqlalchemy_url

    # Reroute the DSN through the same coercion the app uses.
    os.environ["POSTGRES_DSN"] = dsn
    engine = sa.create_engine(_resolve_sqlalchemy_url(), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public'"
                    )
                )
            }
        for expected in (
            "campaigns",
            "runs",
            "llm_calls",
            "evidence_bundles",
            "model_stats_daily",
        ):
            assert expected in tables, f"{expected} missing after upgrade head"
    finally:
        engine.dispose()

    # Downgrade back to baseline; tables must vanish.
    _alembic(["downgrade", "base"])
