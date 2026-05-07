"""Tests for Phase 2.2.3 — Redis pub/sub fan-out of WS broadcasts.

Covers:
* publish-on-broadcast (envelope shape, origin tagging)
* no-op when disabled / when Redis errors
* subscriber filters own-origin messages (no double-delivery)
* subscriber dispatches foreign-origin messages to local clients
* subscriber tolerates malformed envelopes / non-dict payloads

A real-server test under ``redis_real`` exercises a full
publish→subscribe→deliver cycle via two simulated origin IDs.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from core import config, redis_client, runtime

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    runtime._ws_clients.clear()
    redis_client.reset_for_tests()
    # Clear any subscriber thread reference between tests so
    # idempotency tests can assert on start.
    runtime._ws_subscriber_thread = None
    yield
    runtime._ws_clients.clear()
    redis_client.reset_for_tests()
    runtime._ws_subscriber_thread = None


@pytest.fixture
def disabled_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REDIS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "", raising=False)


@pytest.fixture
def fake_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MagicMock]:
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://localhost:6379/0", raising=False)

    fake_client = MagicMock(name="FakeRedisClient")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: fake_client,
    )
    yield fake_client


# ---------------------------------------------------------------------------
# Publish path
# ---------------------------------------------------------------------------


def test_publish_noop_when_disabled(disabled_redis: None) -> None:
    """Publish must be a no-op without Redis (no exception, no side effects)."""
    runtime._publish_ws_broadcast_to_redis({"type": "log", "line": "hi"})


def test_publish_sends_envelope_with_origin(fake_redis: MagicMock) -> None:
    runtime._publish_ws_broadcast_to_redis({"type": "status", "phase": "running"})
    fake_redis.publish.assert_called_once()
    channel, raw = fake_redis.publish.call_args.args
    assert channel == "ws_broadcast"
    envelope = json.loads(raw)
    assert envelope["origin"] == runtime._INSTANCE_ID
    assert envelope["msg"] == {"type": "status", "phase": "running"}


def test_publish_swallows_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    bad_client = MagicMock(name="BoomRedis")
    bad_client.publish.side_effect = redis_pkg.ConnectionError("kaboom")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )
    # Must not raise.
    runtime._publish_ws_broadcast_to_redis({"type": "log"})


def test_ws_broadcast_delivers_locally_and_publishes(
    fake_redis: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_ws_broadcast`` must hit BOTH paths: local delivery and publish."""
    delivered: list[Any] = []

    def fake_local(msg: Any) -> None:
        delivered.append(msg)

    monkeypatch.setattr(runtime, "_deliver_to_local_clients", fake_local)
    runtime._ws_broadcast({"type": "status", "phase": "queued"})

    assert delivered == [{"type": "status", "phase": "queued"}]
    fake_redis.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Subscriber filter / dispatch
# ---------------------------------------------------------------------------


def _drive_loop_with_messages(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[dict[str, Any]],
    fake_client: MagicMock,
) -> list[dict[str, Any]]:
    """Helper: install a fake pubsub that yields ``messages`` then stops,
    invoke ``_ws_subscriber_loop``, and return the list of payloads
    that ``_deliver_to_local_clients`` was called with.
    """
    fake_pubsub = MagicMock(name="FakePubSub")
    fake_pubsub.subscribe = MagicMock()
    fake_pubsub.listen = MagicMock(return_value=iter(messages))
    fake_client.pubsub.return_value = fake_pubsub

    received: list[dict[str, Any]] = []

    def capture(msg: Any) -> None:
        received.append(msg)

    monkeypatch.setattr(runtime, "_deliver_to_local_clients", capture)
    runtime._ws_subscriber_loop()
    return received


def test_subscriber_skips_own_origin(
    fake_redis: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_envelope = {"origin": runtime._INSTANCE_ID, "msg": {"type": "log", "line": "self"}}
    raw_msg = {"type": "message", "data": json.dumps(own_envelope)}

    received = _drive_loop_with_messages(monkeypatch, [raw_msg], fake_redis)
    assert received == []  # filtered out


def test_subscriber_dispatches_foreign_origin(
    fake_redis: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = {"origin": "other-instance", "msg": {"type": "status", "phase": "running"}}
    raw_msg = {"type": "message", "data": json.dumps(foreign)}

    received = _drive_loop_with_messages(monkeypatch, [raw_msg], fake_redis)
    assert received == [{"type": "status", "phase": "running"}]


def test_subscriber_handles_bytes_payload(
    fake_redis: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """redis-py with decode_responses=False yields bytes — subscriber decodes."""
    foreign = {"origin": "other", "msg": {"type": "log"}}
    raw_msg = {"type": "message", "data": json.dumps(foreign).encode("utf-8")}

    received = _drive_loop_with_messages(monkeypatch, [raw_msg], fake_redis)
    assert received == [{"type": "log"}]


def test_subscriber_skips_non_message_events(
    fake_redis: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscribe-confirm events ({type:"subscribe",...}) must not deliver."""
    raw_subscribe = {"type": "subscribe", "data": 1}
    raw_msg = {
        "type": "message",
        "data": json.dumps({"origin": "other", "msg": {"x": 1}}),
    }

    received = _drive_loop_with_messages(
        monkeypatch, [raw_subscribe, raw_msg], fake_redis
    )
    assert received == [{"x": 1}]


def test_subscriber_skips_malformed_json(
    fake_redis: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_bad = {"type": "message", "data": "{not json"}
    raw_good = {
        "type": "message",
        "data": json.dumps({"origin": "other", "msg": {"k": "v"}}),
    }
    received = _drive_loop_with_messages(monkeypatch, [raw_bad, raw_good], fake_redis)
    assert received == [{"k": "v"}]


def test_subscriber_skips_envelope_without_dict_msg(
    fake_redis: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "type": "message",
        "data": json.dumps({"origin": "other", "msg": "not-a-dict"}),
    }
    received = _drive_loop_with_messages(monkeypatch, [raw], fake_redis)
    assert received == []


# ---------------------------------------------------------------------------
# start_ws_broadcast_subscriber
# ---------------------------------------------------------------------------


def test_start_ws_broadcast_subscriber_noop_when_disabled(
    disabled_redis: None,
) -> None:
    runtime.start_ws_broadcast_subscriber()
    assert runtime._ws_subscriber_thread is None


def test_start_ws_broadcast_subscriber_idempotent(
    fake_redis: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling start twice when a thread is alive should not start a second."""
    sentinel = MagicMock(spec=threading.Thread)
    sentinel.is_alive.return_value = True
    runtime._ws_subscriber_thread = sentinel

    started: list[threading.Thread] = []

    real_thread = threading.Thread

    def capturing_ctor(*args: Any, **kwargs: Any) -> threading.Thread:
        t = real_thread(*args, **kwargs)
        started.append(t)
        return t

    monkeypatch.setattr("core.runtime.threading.Thread", capturing_ctor)
    runtime.start_ws_broadcast_subscriber()

    assert started == []  # sentinel was alive — no new thread
    assert runtime._ws_subscriber_thread is sentinel


# ---------------------------------------------------------------------------
# Real-server test (gated by redis_real marker)
# ---------------------------------------------------------------------------


@pytest.mark.redis_real
def test_real_pubsub_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two-process simulation: publish from a 'foreign' origin, verify the
    subscriber on this instance delivers the inner msg to local clients.
    """
    url = os.getenv("REDIS_URL", "")
    assert url, "redis_real marker requires REDIS_URL"
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", url, raising=False)

    received: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runtime,
        "_deliver_to_local_clients",
        lambda msg: received.append(msg),
    )

    # Spin up the real subscriber (against live Redis).
    runtime.start_ws_broadcast_subscriber()
    # Give the thread a moment to SUBSCRIBE; pubsub needs the subscriber
    # to be ACTIVE before publishes are seen.
    time.sleep(0.3)

    # Publish from a foreign origin via a separate connection.
    pub_client = redis_client.get_client()
    foreign_envelope = {
        "origin": "test-foreign-instance",
        "msg": {"type": "status", "phase": "running", "test_marker": "phase2.2.3"},
    }
    pub_client.publish("ws_broadcast", json.dumps(foreign_envelope))

    # Wait up to 2 s for delivery.
    for _ in range(20):
        if received:
            break
        time.sleep(0.1)

    assert any(
        msg.get("test_marker") == "phase2.2.3" for msg in received
    ), f"foreign-origin publish was not delivered (got: {received!r})"
