"""Phase 2.2.4 Redis-backed caches for url and embedding lookups.

Two thin helpers, both fail-tolerant:

* ``url_cache_get_all`` / ``url_cache_store`` — model-name → base-URL
  hash for the Ollama URL resolver. When multiple orchestrator
  instances point at the same Redis, they share the cached result of
  ``/api/tags`` instead of each instance pinging Ollama every TTL
  window.
* ``embed_cache_get`` / ``embed_cache_set`` — keyed by SHA256 of the
  embedded text. When Redis is enabled, embeddings persist
  cross-instance without inflating the JSON file under
  ``memory/embedding_cache.json``.

Both helpers no-op (return ``None`` / no-op) when Redis is disabled,
so callers fall through to the existing in-process / JSON fallback
without branching.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, cast

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key namespaces
# ---------------------------------------------------------------------------

_URL_CACHE_KEY = "ollama_url_cache"
_EMBED_CACHE_PREFIX = "embed_cache:"


def _hash_text(text: str) -> str:
    """Stable identifier for an arbitrary text. SHA256 hex keeps Redis
    keys bounded in length regardless of prompt size."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# URL cache
# ---------------------------------------------------------------------------


def url_cache_get_all() -> dict[str, str] | None:
    """Return the cached model→base-URL map from Redis.

    Returns ``None`` on miss, when Redis is disabled, or on Redis error
    (caller falls through to the in-process refresh path).
    """
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return None
    if not redis_client.is_enabled():
        return None
    try:
        client = redis_client.get_client()
        raw = cast("dict[str, str]", client.hgetall(_URL_CACHE_KEY) or {})
    except Exception as exc:
        log.warning("redis_url_cache_get_failed error=%s", exc)
        return None
    if not raw:
        return None
    return dict(raw)


def url_cache_store(cache: dict[str, str], ttl: int) -> None:
    """Replace the Redis url-cache hash atomically and apply TTL.

    No-op when Redis is disabled. ``DEL + HSET + EXPIRE`` runs in a
    pipeline so the cache never spends time in a half-stale state.
    """
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return
    if not redis_client.is_enabled():
        return
    try:
        client = redis_client.get_client()
        with client.pipeline(transaction=False) as pipe:
            pipe.delete(_URL_CACHE_KEY)
            if cache:
                pipe.hset(_URL_CACHE_KEY, mapping=cache)
            pipe.expire(_URL_CACHE_KEY, ttl)
            pipe.execute()
    except Exception as exc:
        log.warning("redis_url_cache_store_failed error=%s", exc)


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------


def embed_cache_get(text: str) -> list[float] | None:
    """Return the cached embedding vector for ``text`` from Redis, or ``None``.

    Returns ``None`` on miss, when Redis is disabled, on Redis error,
    or on JSON-decode failure (caller falls through to JSON cache).
    """
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return None
    if not redis_client.is_enabled():
        return None
    try:
        client = redis_client.get_client()
        raw = client.get(f"{_EMBED_CACHE_PREFIX}{_hash_text(text)}")
    except Exception as exc:
        log.warning("redis_embed_cache_get_failed error=%s", exc)
        return None
    if raw is None:
        return None
    # redis-py types ``client.get`` as ``Awaitable[T] | T``; the runtime
    # client is sync, so cast narrows the union for ``json.loads``.
    try:
        decoded: Any = json.loads(cast("str | bytes", raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, list):
        return None
    return cast("list[float]", decoded)


def embed_cache_set(text: str, embedding: list[float], ttl: int) -> None:
    """Persist an embedding to Redis with TTL. No-op when disabled."""
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return
    if not redis_client.is_enabled():
        return
    try:
        client = redis_client.get_client()
        client.set(
            f"{_EMBED_CACHE_PREFIX}{_hash_text(text)}",
            json.dumps(embedding),
            ex=ttl,
        )
    except Exception as exc:
        log.warning("redis_embed_cache_set_failed error=%s", exc)
