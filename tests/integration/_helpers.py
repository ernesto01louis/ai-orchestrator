"""Shared helpers for integration tests under tests/integration/.

Kept in a separate module (not conftest.py) so both conftest.py and test
modules can import it without triggering pytest's conftest import restrictions.
"""
from __future__ import annotations

import os


def real_api_url() -> str:
    """Return the real Prefect API URL.

    Prefers $PREFECT_API_URL env var (set by CI or the operator when running
    ``-m prefect_real``), falls back to the loopback address for local dev.
    """
    return os.environ.get("PREFECT_API_URL", "http://127.0.0.1:4200/api").rstrip("/")
