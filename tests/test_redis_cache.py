"""Tests for Phase 2.2.4 Redis-backed url + embedding caches.

Two layers:

* **Mocked / disabled** (default) — every helper must no-op when Redis
  is disabled and swallow Redis errors without raising. Success paths
  use a MagicMock client.
* **redis_real** marker — round-trips against the live LXC 203 Redis.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from core import config, redis_cache, redis_client


@pytest.fixture(autouse=True)
def _reset_redis_singleton() -> Iterator[None]:
    redis_client.reset_for_tests()
    yield
    redis_client.reset_for_tests()


@pytest.fixture
def disabled_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REDIS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "", raising=False)


@pytest.fixture
def fake_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MagicMock]:
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    fake_client = MagicMock(name="FakeRedisClient")
    fake_pipeline = MagicMock(name="FakePipeline")
    fake_pipeline.__enter__ = MagicMock(return_value=fake_pipeline)
    fake_pipeline.__exit__ = MagicMock(return_value=False)
    fake_pipeline.execute.return_value = [True, True, True]
    fake_client.pipeline.return_value = fake_pipeline

    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: fake_client,
    )
    yield fake_client


# ---------------------------------------------------------------------------
# url_cache_get_all / url_cache_store
# ---------------------------------------------------------------------------


def test_url_cache_get_all_returns_none_when_disabled(
    disabled_redis: None,
) -> None:
    assert redis_cache.url_cache_get_all() is None


def test_url_cache_get_all_returns_none_on_empty_hash(
    fake_redis: MagicMock,
) -> None:
    fake_redis.hgetall.return_value = {}
    assert redis_cache.url_cache_get_all() is None


def test_url_cache_get_all_returns_dict_on_hit(fake_redis: MagicMock) -> None:
    fake_redis.hgetall.return_value = {
        "qwen2.5:72b": "http://192.168.2.219:11434",
        "deepseek-coder:33b": "http://192.168.2.216:11434",
    }
    result = redis_cache.url_cache_get_all()
    assert result == {
        "qwen2.5:72b": "http://192.168.2.219:11434",
        "deepseek-coder:33b": "http://192.168.2.216:11434",
    }


def test_url_cache_get_all_swallows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    bad_client = MagicMock()
    bad_client.hgetall.side_effect = redis_pkg.ConnectionError("boom")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )
    assert redis_cache.url_cache_get_all() is None


def test_url_cache_store_noop_when_disabled(disabled_redis: None) -> None:
    redis_cache.url_cache_store({"m": "http://h"}, ttl=60)


def test_url_cache_store_pipelines_del_hset_expire(fake_redis: MagicMock) -> None:
    redis_cache.url_cache_store({"m": "http://h", "n": "http://j"}, ttl=120)
    pipe = fake_redis.pipeline.return_value
    pipe.delete.assert_called_with("ollama_url_cache")
    pipe.hset.assert_called_with(
        "ollama_url_cache", mapping={"m": "http://h", "n": "http://j"}
    )
    pipe.expire.assert_called_with("ollama_url_cache", 120)
    pipe.execute.assert_called_once()


def test_url_cache_store_skips_hset_on_empty_dict(fake_redis: MagicMock) -> None:
    """When the freshly built cache is empty (Ollama unreachable), we
    still DEL + EXPIRE so a stale hash gets cleared, but skip HSET."""
    redis_cache.url_cache_store({}, ttl=60)
    pipe = fake_redis.pipeline.return_value
    pipe.delete.assert_called_with("ollama_url_cache")
    pipe.hset.assert_not_called()
    pipe.expire.assert_called_with("ollama_url_cache", 60)


def test_url_cache_store_swallows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    bad_client = MagicMock()
    bad_client.pipeline.side_effect = redis_pkg.ConnectionError("boom")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )
    redis_cache.url_cache_store({"m": "http://h"}, ttl=60)


# ---------------------------------------------------------------------------
# embed_cache_get / embed_cache_set
# ---------------------------------------------------------------------------


def test_embed_cache_get_none_when_disabled(disabled_redis: None) -> None:
    assert redis_cache.embed_cache_get("hello") is None


def test_embed_cache_get_none_on_miss(fake_redis: MagicMock) -> None:
    fake_redis.get.return_value = None
    assert redis_cache.embed_cache_get("hello") is None


def test_embed_cache_get_returns_decoded_list(fake_redis: MagicMock) -> None:
    fake_redis.get.return_value = json.dumps([0.1, 0.2, 0.3])
    assert redis_cache.embed_cache_get("hello") == [0.1, 0.2, 0.3]


def test_embed_cache_get_returns_none_on_garbage_json(
    fake_redis: MagicMock,
) -> None:
    fake_redis.get.return_value = "not-json"
    assert redis_cache.embed_cache_get("hello") is None


def test_embed_cache_get_returns_none_on_non_list_payload(
    fake_redis: MagicMock,
) -> None:
    """A defensively-stored non-list value (shouldn't happen in prod but
    defends against poisoned cache) returns None — caller falls back."""
    fake_redis.get.return_value = json.dumps({"unexpected": "shape"})
    assert redis_cache.embed_cache_get("hello") is None


def test_embed_cache_get_swallows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    bad_client = MagicMock()
    bad_client.get.side_effect = redis_pkg.ConnectionError("boom")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )
    assert redis_cache.embed_cache_get("hello") is None


def test_embed_cache_set_noop_when_disabled(disabled_redis: None) -> None:
    redis_cache.embed_cache_set("hello", [0.1, 0.2], ttl=3600)


def test_embed_cache_set_writes_with_ttl(fake_redis: MagicMock) -> None:
    redis_cache.embed_cache_set("hello", [0.1, 0.2, 0.3], ttl=3600)
    fake_redis.set.assert_called_once()
    key, payload = fake_redis.set.call_args.args
    assert key.startswith("embed_cache:")
    assert json.loads(payload) == [0.1, 0.2, 0.3]
    assert fake_redis.set.call_args.kwargs["ex"] == 3600


def test_embed_cache_set_uses_stable_hash(fake_redis: MagicMock) -> None:
    """Same text → same key. Different text → different key."""
    redis_cache.embed_cache_set("alpha", [0.1], ttl=3600)
    key_a = fake_redis.set.call_args.args[0]

    fake_redis.set.reset_mock()
    redis_cache.embed_cache_set("alpha", [0.1], ttl=3600)
    key_a2 = fake_redis.set.call_args.args[0]

    fake_redis.set.reset_mock()
    redis_cache.embed_cache_set("beta", [0.2], ttl=3600)
    key_b = fake_redis.set.call_args.args[0]

    assert key_a == key_a2
    assert key_a != key_b


def test_embed_cache_set_swallows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    bad_client = MagicMock()
    bad_client.set.side_effect = redis_pkg.ConnectionError("boom")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )
    redis_cache.embed_cache_set("hello", [0.1], ttl=3600)


# ---------------------------------------------------------------------------
# Real-server tests (gated by redis_real marker)
# ---------------------------------------------------------------------------


@pytest.mark.redis_real
def test_real_url_cache_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.getenv("REDIS_URL", "")
    assert url, "redis_real marker requires REDIS_URL"
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", url, raising=False)

    sample = {"m1": "http://a:11434", "m2": "http://b:11434"}
    try:
        redis_cache.url_cache_store(sample, ttl=60)
        loaded = redis_cache.url_cache_get_all()
        assert loaded == sample

        client = redis_client.get_client()
        ttl: Any = client.ttl("ollama_url_cache")
        assert 0 < int(ttl) <= 60
    finally:
        redis_client.get_client().delete("ollama_url_cache")


@pytest.mark.redis_real
def test_real_embed_cache_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.getenv("REDIS_URL", "")
    assert url, "redis_real marker requires REDIS_URL"
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", url, raising=False)

    text = "ai-orchestrator phase 2.2.4 sentinel"
    embedding = [0.1, -0.2, 0.3, -0.4, 0.5]
    try:
        assert redis_cache.embed_cache_get(text) is None  # cold start

        redis_cache.embed_cache_set(text, embedding, ttl=60)
        loaded = redis_cache.embed_cache_get(text)
        assert loaded == embedding
    finally:
        # Belt-and-braces cleanup so test re-runs are repeatable.
        import hashlib  # noqa: PLC0415
        client = redis_client.get_client()
        client.delete(
            f"embed_cache:{hashlib.sha256(text.encode()).hexdigest()}"
        )
