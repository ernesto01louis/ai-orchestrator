"""Tests for Phase 2.2 Redis mirror of RUN_STATUS in core.runtime.

Two layers (mirrors tests/test_db.py / tests/test_redis_client.py):

* **Mocked / disabled** (default suite) — verifies that:
    - mirror is a no-op when Redis is disabled
    - mirror writes hash + EXPIRE when enabled
    - mirror swallows redis errors (RUN_STATUS still mutated)
    - hydrate_run_status_from_redis() repopulates the in-process dict
    - hydrate handles redis errors gracefully

* **redis_real** marker — runs against live Redis. Verifies an
  init/update sequence round-trips through Redis hashes back into
  RUN_STATUS via hydrate.
"""
from __future__ import annotations

import json
import os
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
    """Each test starts with empty RUN_STATUS + no cached redis client."""
    runtime.RUN_STATUS.clear()
    redis_client.reset_for_tests()
    yield
    runtime.RUN_STATUS.clear()
    redis_client.reset_for_tests()


@pytest.fixture
def disabled_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REDIS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "", raising=False)


@pytest.fixture
def fake_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MagicMock]:
    """Redis reports enabled; from_url returns a MagicMock client.

    Yields the mock so tests can assert on hset/expire/hgetall/scan_iter
    interactions and inject side-effects.
    """
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://localhost:6379/0", raising=False)
    monkeypatch.setattr(config, "REDIS_RUN_STATUS_TTL", 86400, raising=False)

    fake_client = MagicMock(name="FakeRedisClient")
    # Pipeline is a context manager; configure it to return a child mock
    # whose execute() returns whatever; calls captured for assertions.
    fake_pipeline = MagicMock(name="FakeRedisPipeline")
    fake_pipeline.__enter__ = MagicMock(return_value=fake_pipeline)
    fake_pipeline.__exit__ = MagicMock(return_value=False)
    fake_pipeline.execute.return_value = [True, True]
    fake_client.pipeline.return_value = fake_pipeline

    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: fake_client,
    )
    yield fake_client


# ---------------------------------------------------------------------------
# Mirror writes
# ---------------------------------------------------------------------------


def test_mirror_noop_when_redis_disabled(disabled_redis: None) -> None:
    """No Redis call should happen when redis.enabled=false."""
    # The function returns silently — we just confirm no exception.
    runtime._mirror_run_status_to_redis(
        "run-x", {"phase": "queued"}, operation="init"
    )
    # And the in-process dict isn't touched by the mirror.
    assert "run-x" not in runtime.RUN_STATUS


def test_mirror_pipelines_hset_expire_when_enabled(fake_redis: MagicMock) -> None:
    snapshot = {"phase": "running", "score": 7.5, "completed": False}
    runtime._mirror_run_status_to_redis("run-1", snapshot, operation="update")

    pipeline = fake_redis.pipeline.return_value
    # hset called with the JSON-encoded mapping
    hset_args = pipeline.hset.call_args
    assert hset_args.args[0] == "run_status:run-1"
    encoded = hset_args.kwargs["mapping"]
    assert json.loads(encoded["phase"]) == "running"
    assert json.loads(encoded["score"]) == 7.5
    assert json.loads(encoded["completed"]) is False
    # expire called with the configured TTL
    pipeline.expire.assert_called_with("run_status:run-1", 86400)
    pipeline.execute.assert_called_once()


def test_mirror_swallows_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A boom from the pipeline must not propagate."""
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)
    monkeypatch.setattr(config, "REDIS_RUN_STATUS_TTL", 86400, raising=False)

    bad_client = MagicMock(name="BoomRedis")
    bad_client.pipeline.side_effect = redis_pkg.ConnectionError("boom")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )
    # Must not raise.
    runtime._mirror_run_status_to_redis(
        "run-x", {"phase": "queued"}, operation="init"
    )


# ---------------------------------------------------------------------------
# _init_run_status / _update_run_status integration
# ---------------------------------------------------------------------------


def test_init_run_status_calls_mirror(fake_redis: MagicMock) -> None:
    runtime._init_run_status("run-init-1", project="proj", target="tgt")

    pipeline = fake_redis.pipeline.return_value
    assert pipeline.hset.call_args.args[0] == "run_status:run-init-1"
    encoded = pipeline.hset.call_args.kwargs["mapping"]
    assert json.loads(encoded["phase"]) == "queued"
    assert json.loads(encoded["project"]) == "proj"


def test_update_run_status_calls_mirror(fake_redis: MagicMock) -> None:
    runtime._init_run_status("run-upd-1")
    fake_redis.pipeline.return_value.hset.reset_mock()

    runtime._update_run_status("run-upd-1", phase="running", score=8.0)

    encoded = fake_redis.pipeline.return_value.hset.call_args.kwargs["mapping"]
    assert json.loads(encoded["phase"]) == "running"
    assert json.loads(encoded["score"]) == 8.0


def test_init_run_status_in_process_dict_still_works_when_disabled(
    disabled_redis: None,
) -> None:
    """Sanity — disabling Redis must not break the in-process semantics."""
    runtime._init_run_status("run-z")
    assert runtime.RUN_STATUS["run-z"]["phase"] == "queued"
    runtime._update_run_status("run-z", phase="running")
    assert runtime.RUN_STATUS["run-z"]["phase"] == "running"


# ---------------------------------------------------------------------------
# hydrate_run_status_from_redis
# ---------------------------------------------------------------------------


def test_hydrate_noop_when_redis_disabled(disabled_redis: None) -> None:
    assert runtime.hydrate_run_status_from_redis() == 0


def test_hydrate_repopulates_run_status(fake_redis: MagicMock) -> None:
    fake_redis.scan_iter.return_value = iter(
        ["run_status:r1", "run_status:r2"]
    )
    fake_redis.hgetall.side_effect = [
        {
            "phase": json.dumps("running"),
            "score": json.dumps(0),
            "completed": json.dumps(False),
            "project": json.dumps("alpha"),
        },
        {
            "phase": json.dumps("completed"),
            "score": json.dumps(9.5),
            "completed": json.dumps(True),
        },
    ]
    count = runtime.hydrate_run_status_from_redis()
    assert count == 2
    assert runtime.RUN_STATUS["r1"]["phase"] == "running"
    assert runtime.RUN_STATUS["r1"]["project"] == "alpha"
    assert runtime.RUN_STATUS["r2"]["completed"] is True
    assert runtime.RUN_STATUS["r2"]["score"] == 9.5


def test_hydrate_skips_empty_hashes(fake_redis: MagicMock) -> None:
    fake_redis.scan_iter.return_value = iter(["run_status:gone"])
    fake_redis.hgetall.return_value = {}
    assert runtime.hydrate_run_status_from_redis() == 0
    assert "gone" not in runtime.RUN_STATUS


def test_hydrate_swallows_scan_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis as redis_pkg  # noqa: PLC0415

    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", "redis://h/0", raising=False)

    bad_client = MagicMock(name="ScanBoomRedis")
    bad_client.scan_iter.side_effect = redis_pkg.ConnectionError("boom")
    monkeypatch.setattr(
        "core.redis_client.redis.Redis.from_url",
        lambda *_a, **_k: bad_client,
    )

    assert runtime.hydrate_run_status_from_redis() == 0


def test_hydrate_skips_field_with_garbage_json(fake_redis: MagicMock) -> None:
    """A non-JSON field value must not crash hydrate — fall back to raw str."""
    fake_redis.scan_iter.return_value = iter(["run_status:weird"])
    fake_redis.hgetall.return_value = {
        "phase": "not-json",  # missing quotes — json.loads fails
        "score": json.dumps(3),
    }
    count = runtime.hydrate_run_status_from_redis()
    assert count == 1
    assert runtime.RUN_STATUS["weird"]["phase"] == "not-json"
    assert runtime.RUN_STATUS["weird"]["score"] == 3


# ---------------------------------------------------------------------------
# Real-server tests (gated by redis_real marker)
# ---------------------------------------------------------------------------


@pytest.mark.redis_real
def test_real_init_update_round_trips_through_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init → update → clear in-process → hydrate → identical state."""
    url = os.getenv("REDIS_URL", "")
    assert url, "redis_real marker requires REDIS_URL"
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", url, raising=False)
    monkeypatch.setattr(config, "REDIS_RUN_STATUS_TTL", 60, raising=False)

    run_id = "ai-orchestrator:test:phase2.2:roundtrip-runtime"
    try:
        runtime._init_run_status(run_id, project="testproj", target="testhost")
        runtime._update_run_status(run_id, phase="running", score=4.2)

        original = dict(runtime.RUN_STATUS[run_id])
        runtime.RUN_STATUS.clear()

        count = runtime.hydrate_run_status_from_redis()
        assert count >= 1
        assert run_id in runtime.RUN_STATUS
        # The hydrated entry must reflect the most recent update.
        assert runtime.RUN_STATUS[run_id]["phase"] == "running"
        assert runtime.RUN_STATUS[run_id]["score"] == 4.2
        assert runtime.RUN_STATUS[run_id]["project"] == "testproj"
        # And the round-trip must preserve the full snapshot.
        for field in ("phase", "score", "project", "target", "completed"):
            assert runtime.RUN_STATUS[run_id][field] == original.get(field)
    finally:
        # Clean up Redis side-effects — no orphan keys leaking into prod.
        client = redis_client.get_client()
        client.delete(f"run_status:{run_id}")


@pytest.mark.redis_real
def test_real_mirror_sets_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a mirror write, the key's TTL must reflect REDIS_RUN_STATUS_TTL."""
    url = os.getenv("REDIS_URL", "")
    assert url, "redis_real marker requires REDIS_URL"
    monkeypatch.setattr(config, "REDIS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REDIS_URL", url, raising=False)
    monkeypatch.setattr(config, "REDIS_RUN_STATUS_TTL", 60, raising=False)

    run_id = "ai-orchestrator:test:phase2.2:ttl-check"
    key = f"run_status:{run_id}"
    try:
        runtime._init_run_status(run_id)
        client = redis_client.get_client()
        ttl: Any = client.ttl(key)
        # ttl must be in (0, 60] — the EXPIRE was just set to 60 seconds.
        assert 0 < int(ttl) <= 60
    finally:
        client = redis_client.get_client()
        client.delete(key)
