"""Phase 2.2 — Redis ephemeral state store (client + helpers).

Parallel of ``core.db`` for Redis. Intentionally tiny and dormant by
default:

* ``is_enabled()`` — single source of truth for "should we attempt
  Redis ops?". Returns ``False`` when ``redis.enabled=false`` in
  config.json or the resolved URL is empty.
* ``get_client()`` — lazily constructs and caches a sync ``redis.Redis``
  client. Connection-pool defaults from redis-py are fine here; we only
  set socket timeouts (so a wedged Redis can't stall request handlers)
  and ``decode_responses=True`` (callers think in ``str``, not ``bytes``).
* ``ping()`` — convenience wrapper for health checks. Returns ``False``
  on any error (never raises) so a paused/down Redis doesn't take down
  ``/health``.
* ``reset_for_tests()`` — drops cached client so tests can re-init
  against different config values.

Phase 2.2 callsites (RUN_STATUS, ws pub/sub, URL cache, embedding cache)
treat Redis as a coordination/cache layer: process-local state stays
authoritative, Redis is failure-tolerant. Hot paths must check
``is_enabled()`` first and degrade gracefully when ``False``.

Driver: ``redis-py`` 7.x sync. Async would propagate ``await`` through
``core/runtime`` and ``llm/ollama`` for no real win — every callsite is
already inside a sync FastAPI handler or a Prefect ``@task`` body
(matches the rationale in core/db.py).
"""
from __future__ import annotations

import logging
import threading

import redis
from redis import Redis

from core import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton (lazy-initialized, thread-safe)
# ---------------------------------------------------------------------------

_client: Redis | None = None
_init_lock = threading.RLock()


def is_enabled() -> bool:
    """Return whether Redis is wired up.

    True only when both ``redis.enabled=true`` in config.json AND a
    non-empty URL is resolvable (env var ``REDIS_URL`` wins over the
    config.json value).
    """
    return bool(config.REDIS_ENABLED and config.REDIS_URL)


def get_client() -> Redis:
    """Return the cached Redis client, building it on first call.

    Raises ``RuntimeError`` if Redis is not enabled — callers must
    check :func:`is_enabled` first (loud failure beats a silent no-op
    when a callsite is misconfigured).
    """
    global _client
    if _client is not None:
        return _client

    if not is_enabled():
        raise RuntimeError(
            "core.redis_client.get_client() called with Redis disabled — "
            "check is_enabled() before requesting a client."
        )

    with _init_lock:
        if _client is not None:  # double-checked locking
            return _client
        _client = redis.Redis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=config.REDIS_SOCKET_CONNECT_TIMEOUT,
            socket_timeout=config.REDIS_SOCKET_TIMEOUT,
            health_check_interval=30,
        )
        return _client


def ping() -> bool:
    """Light health check. Returns ``False`` on any error.

    Used by ``/health`` (and the Phase 2.2 smoke tests) to surface
    Redis reachability without raising. Returns ``False`` when Redis
    is disabled — a disabled Redis is not "unhealthy", but the caller
    can distinguish via ``is_enabled()`` if they need to.
    """
    if not is_enabled():
        return False
    try:
        return bool(get_client().ping())
    except redis.RedisError as exc:
        log.warning("redis ping failed: %s", exc)
        return False


def reset_for_tests() -> None:
    """Drop the cached client.

    Tests that monkeypatch ``config.REDIS_ENABLED`` / ``REDIS_URL`` must
    call this first, otherwise the cached singleton keeps the previous
    config alive.
    """
    global _client
    with _init_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # pragma: no cover — defensive
                pass
        _client = None
