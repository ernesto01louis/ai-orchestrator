"""Fixtures for real-Prefect-server integration tests.

The session-scoped ``_prefect_test_harness`` autouse fixture in the parent
conftest wraps the entire test suite in an in-memory SQLite Prefect server.
Tests in this directory need to reach the REAL Prefect server instead.

We override that by providing a function-scoped ``_prefect_test_harness``
fixture here (pytest prefers the closest-scope fixture when the name
conflicts).  However, because the session-scoped fixture has already started
the harness by the time function-scope runs, we cannot stop it.  What we CAN
do is temporarily push Prefect's settings back to the real server URL for each
test using ``prefect.settings.temporary_settings``.  This ensures that any
Prefect @flow/@task calls made during these tests submit to the real server,
not the harness's SQLite instance.

Only active when the ``prefect_real`` marker is selected (``-m prefect_real``).
"""
from __future__ import annotations

import pytest

from tests.integration._helpers import real_api_url


@pytest.fixture(autouse=True)
def _real_server_settings():
    """Temporarily point the Prefect client at the real server for this test.

    This overrides the harness's temporary_settings context for the duration
    of each test in this directory, so @flow invocations go to the real server.

    Note: the session-scoped _prefect_test_harness (root conftest) pushes its
    own settings context for the SQLite test instance.  We push a nested
    temporary_settings here to override PREFECT_API_URL back to the real server
    for this directory's tests.  If a future Prefect version changes how
    settings contexts stack, this override may need to be replaced with explicit
    ``get_client(api_url=...)`` calls per test.
    """
    from prefect.settings import PREFECT_API_URL, temporary_settings

    with temporary_settings(updates={PREFECT_API_URL: real_api_url()}):
        yield
