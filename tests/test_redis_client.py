"""Tests for core.redis_client — Redis client + helpers (Phase 2.2).

Two layers, mirroring tests/test_db.py:

* **Mocked / disabled** (default suite) — verifies that:
    - is_enabled() reflects config flags
    - get_client() raises loudly when disabled
    - Client is built lazily and cached
    - ping() never raises and returns False when disabled

* **redis_real** marker — runs only when CI or an operator points
  $REDIS_URL at a live Redis. Verifies a real round-trip.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from core import config, redis_client

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_redis_singletons() -> Iterator[None]:
    """Each test starts with no cached client — different tests use
    different config values."""
    redis_client.reset_for_tests()
    yield
    redis_client.reset_for_tests()


@pytest.fixture
def disabled_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REDIS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "", raising=False)


@pytest.fixture
def enabled_mocked_config(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Redis reports as enabled with a fake URL; ``redis.Redis.from_url``
    is patched so the cache/singleton tests don't try to actually
    connect anywhere. Real connection behavior lives under the
    ``redis_real`` marker.
    """
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://localhost:6379/0", raising=False)
    monkeypatch.setattr(config, "REDIS_SOCKET_CONNECT_TIMEOUT", 2.0, raising=False)
    monkeypatch.setattr(config, "REDIS_SOCKET_TIMEOUT", 5.0, raising=False)

    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        return MagicMock(name="MockRedisClient")

    monkeypatch.setattr("core.redis_client.redis.Redis.from_url", _factory)
    yield


# ---------------------------------------------------------------------------
# is_enabled()
# ---------------------------------------------------------------------------


def test_is_enabled_false_by_default(disabled_config: None) -> None:
    assert redis_client.is_enabled() is False


def test_is_enabled_false_when_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "", raising=False)
    assert redis_client.is_enabled() is False


def test_is_enabled_true_with_url_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://localhost:6379/0", raising=False)
    assert redis_client.is_enabled() is True


# ---------------------------------------------------------------------------
# Disabled-mode safety: every entry point must raise loudly OR no-op
# ---------------------------------------------------------------------------


def test_get_client_raises_when_disabled(disabled_config: None) -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        redis_client.get_client()


def test_ping_returns_false_when_disabled(disabled_config: None) -> None:
    """ping() must never raise — disabled = False, not exception."""
    assert redis_client.ping() is False


# ---------------------------------------------------------------------------
# Client caching (uses MagicMock so we never hit a network)
# ---------------------------------------------------------------------------


def test_get_client_caches_singleton(enabled_mocked_config: None) -> None:
    c1 = redis_client.get_client()
    c2 = redis_client.get_client()
    assert c1 is c2


def test_reset_for_tests_releases_singleton(enabled_mocked_config: None) -> None:
    c1 = redis_client.get_client()
    redis_client.reset_for_tests()
    c2 = redis_client.get_client()
    assert c1 is not c2


def test_get_client_passes_socket_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify socket_connect_timeout / socket_timeout / decode_responses
    flow into the client constructor."""
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)
    monkeypatch.setattr(config, "REDIS_SOCKET_CONNECT_TIMEOUT", 1.5, raising=False)
    monkeypatch.setattr(config, "REDIS_SOCKET_TIMEOUT", 4.5, raising=False)

    captured: dict[str, Any] = {}

    def _factory(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured.update(kwargs)
        return MagicMock(name="MockRedisClient")

    monkeypatch.setattr("core.redis_client.redis.Redis.from_url", _factory)
    redis_client.get_client()

    assert captured["url"] == "redis://h/0"
    assert captured["socket_connect_timeout"] == 1.5
    assert captured["socket_timeout"] == 4.5
    assert captured["decode_responses"] is True


# ---------------------------------------------------------------------------
# ping() failure semantics
# ---------------------------------------------------------------------------


def test_ping_swallows_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """ping() must catch redis.RedisError and return False, not propagate."""
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    bad_client = MagicMock(name="UnreachableRedis")
    bad_client.ping.side_effect = redis_pkg.ConnectionError("kaboom")

    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )
    assert redis_client.ping() is False


def test_ping_returns_true_on_pong(enabled_mocked_config: None) -> None:
    client = redis_client.get_client()
    client.ping = MagicMock(return_value=True)
    assert redis_client.ping() is True


# ---------------------------------------------------------------------------
# Real-server tests (gated by redis_real marker)
# ---------------------------------------------------------------------------


@pytest.mark.redis_real
def test_real_client_round_trips_set_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: real Redis, ``SET`` then ``GET`` round-trips."""
    url = os.getenv("REDIS_URL", "")
    assert url, "redis_real marker requires REDIS_URL"
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", url, raising=False)

    client = redis_client.get_client()
    key = "ai-orchestrator:test:phase2.2:roundtrip"
    try:
        client.set(key, "ok", ex=30)
        assert client.get(key) == "ok"
    finally:
        client.delete(key)


@pytest.mark.redis_real
def test_real_ping_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.getenv("REDIS_URL", "")
    assert url, "redis_real marker requires REDIS_URL"
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", url, raising=False)

    assert redis_client.ping() is True
