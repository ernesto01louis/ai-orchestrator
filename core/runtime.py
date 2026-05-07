"""Runtime state — RUN_STATUS dict, WebSocket broadcast, log writer.

These three concerns are tightly coupled (log writes status, status writes
broadcast, broadcast pushes to WS clients) so they live in one file. Any
thread can call into here safely:

  * `_ws_broadcast` posts coroutines onto the captured main loop via
    `asyncio.run_coroutine_threadsafe`.
  * `_update_run_status` and `_init_run_status` use a threading.Lock to
    serialize mutations.
  * `log` writes via flock on the per-run log file.

The main loop is captured by `_lifespan` at startup via `set_main_loop`.

Phase 2.2: when ``redis.enabled=true``, every ``_init_run_status`` /
``_update_run_status`` write is mirrored to Redis (best-effort, never
raises). At startup, ``hydrate_run_status_from_redis()`` repopulates
the in-process dict from Redis so in-flight runs survive an
orchestrator restart. The in-process dict remains the hot-path read
source — Redis is a write-through mirror, not the canonical store.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .paths import LOG_DIR, RUN_INDEX_FILE

_logger = logging.getLogger(__name__)

# ── live process state ─────────────────────────────
RUN_STATUS: dict = {}
_run_status_lock = threading.Lock()
ORCHESTRATOR_PAUSED = False

# Per-campaign live flags (Phase 1.1). Keyed by campaign_id; entries:
#   {"phase": "queued"|"running"|"paused"|"completed"|"aborted"|"failed",
#    "paused": bool, "aborted": bool, "current_run_id": str | None,
#    "manifest_status": "ok"|"corrupted"|"missing"|"skipped"|None}
#      - "ok" / "skipped" set by run_campaign hook at end of campaign  (Phase C)
#      - "corrupted" / "missing" set by /campaigns/{id}/verify-merkle  (Phase D)
CAMPAIGN_STATUS: dict = {}
_campaign_status_lock = threading.Lock()

_ws_clients: list = []
_ws_lock = threading.Lock()
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None

# Redis hash key prefix for the RUN_STATUS mirror. Keys look like
# ``run_status:<run_id>``; values are HSETs with one field per RUN_STATUS
# entry, JSON-encoded so mixed types round-trip.
_REDIS_RUN_STATUS_PREFIX = "run_status:"

# Phase 2.2.3 — pub/sub channel for cross-instance WebSocket fan-out.
# When ``redis.enabled=true``, every ``_ws_broadcast`` call publishes
# here; the subscriber thread on each orchestrator instance consumes
# from the channel and re-delivers to its local _ws_clients. The
# ``origin`` field on each envelope keeps an instance from re-delivering
# its own publish (avoids the self-loop).
_WS_BROADCAST_CHANNEL = "ws_broadcast"

# Per-process unique ID. ``ORCHESTRATOR_INSTANCE_ID`` env override is
# accepted so operators can pin known IDs in multi-instance setups; the
# random fallback covers single-instance dev.
_INSTANCE_ID = os.environ.get("ORCHESTRATOR_INSTANCE_ID") or secrets.token_hex(8)

# Subscriber thread handle (started by ``start_ws_broadcast_subscriber``,
# called from app.py:_lifespan once Redis is confirmed enabled).
_ws_subscriber_thread: threading.Thread | None = None
_ws_subscriber_lock = threading.Lock()


def _redis_run_status_key(run_id: str) -> str:
    return f"{_REDIS_RUN_STATUS_PREFIX}{run_id}"


def _mirror_run_status_to_redis(
    run_id: str, snapshot: dict[str, Any], *, operation: str
) -> None:
    """Best-effort write of a RUN_STATUS snapshot to Redis. Never raises.

    No-op when Redis is disabled. On failure, logs a structured WARN and
    bumps the Prometheus counter; in-process state stays canonical.
    ``operation`` is one of ``"init"`` / ``"update"`` and tags the metric.
    """
    # Lazy imports — avoid pulling redis-py / prometheus_client into
    # core.runtime at module-load time. Both modules are intentionally
    # late-bound so a missing dependency never blocks orchestrator boot.
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return
    if not redis_client.is_enabled():
        return
    try:
        client = redis_client.get_client()
        encoded = {k: json.dumps(v, default=str) for k, v in snapshot.items()}
        key = _redis_run_status_key(run_id)
        # HSET + EXPIRE in a pipeline so the TTL doesn't lag a slow
        # round-trip. The pipeline is implicit MULTI/EXEC under the hood.
        from core import config as _config  # noqa: PLC0415
        with client.pipeline(transaction=False) as pipe:
            pipe.hset(key, mapping=encoded)
            pipe.expire(key, _config.REDIS_RUN_STATUS_TTL)
            pipe.execute()
        _observe_redis_run_status(operation, success=True)
    except Exception as exc:
        _logger.warning(
            "redis_run_status_mirror_failed run_id=%s operation=%s error=%s",
            run_id, operation, exc,
        )
        _observe_redis_run_status(operation, success=False)


def _observe_redis_run_status(operation: str, *, success: bool) -> None:
    try:
        from core.metrics import observe_redis_run_status_write  # noqa: PLC0415
        observe_redis_run_status_write(operation, success=success)
    except Exception:
        pass


def hydrate_run_status_from_redis() -> int:
    """Repopulate ``RUN_STATUS`` from Redis at startup. Returns the
    count of runs hydrated.

    No-op when Redis is disabled or unreachable. Existing in-process
    entries (which shouldn't exist at startup but may in tests) are
    overwritten. Called from ``app.py:_lifespan``; the orchestrator
    boots even if hydration fails.
    """
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return 0
    if not redis_client.is_enabled():
        return 0
    try:
        client = redis_client.get_client()
        keys: list[str] = list(
            client.scan_iter(match=f"{_REDIS_RUN_STATUS_PREFIX}*", count=200)
        )
    except Exception as exc:
        _logger.warning("redis_run_status_hydrate_scan_failed error=%s", exc)
        _observe_redis_run_status("hydrate", success=False)
        return 0

    count = 0
    with _run_status_lock:
        for key in keys:
            try:
                # cast() narrows redis-py's ``ResponseT = Awaitable[T] | T``
                # return-type union to the sync branch; the runtime client
                # is always sync (constructed in core.redis_client).
                raw = cast("dict[str, str]", client.hgetall(key) or {})
            except Exception as exc:
                _logger.warning(
                    "redis_run_status_hydrate_hgetall_failed key=%s error=%s",
                    key, exc,
                )
                continue
            if not raw:
                continue
            run_id = key[len(_REDIS_RUN_STATUS_PREFIX):]
            decoded: dict[str, Any] = {}
            for k, v in raw.items():
                try:
                    decoded[k] = json.loads(v)
                except (TypeError, ValueError):
                    decoded[k] = v
            RUN_STATUS[run_id] = decoded
            count += 1
    _observe_redis_run_status("hydrate", success=True)
    if count:
        _logger.info("redis_run_status_hydrated count=%s", count)
    return count


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Captured by `_lifespan` at startup so background threads can post
    coroutines back onto the FastAPI event loop via run_coroutine_threadsafe.
    """
    global _MAIN_LOOP
    _MAIN_LOOP = loop


# ── WebSocket broadcast ────────────────────────────
def _ws_broadcast(msg: dict) -> None:
    """Send a JSON message to all connected WebSocket clients.

    Safe to call from any thread: the actual `ws.send_text` coroutine is
    scheduled on the captured main loop via asyncio.run_coroutine_threadsafe.
    Each send is bounded by a 2 s timeout; clients that error are evicted.

    Phase 2.2.3: when Redis is enabled, the message is also published
    on ``_WS_BROADCAST_CHANNEL`` so other orchestrator instances can
    fan it out to their local clients. Local delivery happens
    unconditionally — Redis is purely additive.
    """
    _deliver_to_local_clients(msg)
    _publish_ws_broadcast_to_redis(msg)


def _deliver_to_local_clients(msg: dict[str, Any]) -> None:
    """Deliver a broadcast message to clients connected to THIS process.

    Extracted so the Redis subscriber thread can re-use the same
    delivery semantics for messages originated on other instances.
    """
    if _MAIN_LOOP is None:
        # startup hasn't completed; no clients are subscribed yet anyway
        return
    with _ws_lock:
        clients = list(_ws_clients)
    if not clients:
        return
    payload = json.dumps(msg)
    dead: list = []
    for ws in clients:
        try:
            fut = asyncio.run_coroutine_threadsafe(ws.send_text(payload), _MAIN_LOOP)
            fut.result(timeout=2.0)
        except Exception:
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                try:
                    _ws_clients.remove(ws)
                except ValueError:
                    pass


def _publish_ws_broadcast_to_redis(msg: dict[str, Any]) -> None:
    """Publish a broadcast envelope to the Redis fan-out channel.

    Best-effort; never raises. No-op when Redis is disabled. The
    envelope carries an ``origin`` field so the subscriber on this
    instance can ignore its own publishes (avoiding double-delivery).
    """
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return
    if not redis_client.is_enabled():
        return
    try:
        envelope = {"origin": _INSTANCE_ID, "msg": msg}
        client = redis_client.get_client()
        client.publish(_WS_BROADCAST_CHANNEL, json.dumps(envelope))
    except Exception as exc:
        _logger.warning("redis_ws_publish_failed error=%s", exc)


def start_ws_broadcast_subscriber() -> None:
    """Start the daemon thread that subscribes to the WS broadcast channel.

    No-op when Redis is disabled or the thread is already running.
    Idempotent — safe to call from ``_lifespan`` even on hot-reload.

    The thread filters out envelopes originated by this instance
    (matching ``_INSTANCE_ID``) so a single-process setup doesn't
    deliver each message twice.
    """
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return
    if not redis_client.is_enabled():
        return

    global _ws_subscriber_thread
    with _ws_subscriber_lock:
        if _ws_subscriber_thread is not None and _ws_subscriber_thread.is_alive():
            return
        thread = threading.Thread(
            target=_ws_subscriber_loop,
            name="redis-ws-subscriber",
            daemon=True,
        )
        _ws_subscriber_thread = thread
        thread.start()


def _ws_subscriber_loop() -> None:
    """Long-running pub/sub consumer. Logs and exits on fatal Redis error.

    Daemon thread — Python lets it terminate on process shutdown.
    """
    try:
        from core import redis_client  # noqa: PLC0415
    except ImportError:
        return
    try:
        client = redis_client.get_client()
        # redis-py's PubSub factory is untyped — its constructor takes
        # dynamic kwargs that mypy can't see through, so we suppress.
        pubsub = client.pubsub()  # type: ignore[no-untyped-call]
        pubsub.subscribe(_WS_BROADCAST_CHANNEL)
    except Exception as exc:
        _logger.warning("redis_ws_subscribe_failed error=%s", exc)
        return
    # Poll-based loop instead of ``pubsub.listen()`` — the connection
    # inherits the client's ``socket_timeout`` (5s by default), and a
    # blocking listen() raises on every timeout window. ``get_message``
    # with a bounded timeout returns ``None`` cleanly when there's
    # nothing to read, letting the loop survive idle pub/sub forever.
    while True:
        try:
            raw = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        except Exception as exc:  # pragma: no cover — connection death
            _logger.warning("redis_ws_subscriber_died error=%s", exc)
            return
        if raw is None:
            continue
        if raw.get("type") != "message":
            continue
        data = raw.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        try:
            envelope = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(envelope, dict):
            continue
        if envelope.get("origin") == _INSTANCE_ID:
            continue  # don't re-deliver our own publish
        inner = envelope.get("msg")
        if isinstance(inner, dict):
            _deliver_to_local_clients(inner)


# ── run status helpers ─────────────────────────────
def _load_run_index() -> dict:
    """Load the persistent run index from disk."""
    try:
        with open(RUN_INDEX_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _persist_run_index(run_id: str, snapshot: dict) -> None:
    """Save a completed run's metadata to the persistent run index.

    Phase 2.1: after the JSON write succeeds, mirror the row into
    Postgres via core.db_writethrough. JSON stays canonical — a Postgres
    write failure is logged + swallowed.
    """
    try:
        index = _load_run_index()
        index[run_id] = {
            "phase": snapshot.get("phase", "completed"),
            "score": snapshot.get("score", 0),
            "completed": True,
            "project": snapshot.get("project", ""),
            "target": snapshot.get("target", ""),
            "has_error": snapshot.get("error") is not None,
            "error_msg": str(snapshot["error"])[:200] if snapshot.get("error") else None,
            "timestamp": datetime.utcnow().isoformat(),
        }
        with open(RUN_INDEX_FILE, "w") as f:
            json.dump(index, f, indent=2)
    except OSError:
        return
    # Lazy import — core.runtime is loaded early in app boot; we don't
    # want to drag SQLAlchemy in unless someone actually persists a run.
    try:
        from core import db_writethrough
        db_writethrough.mirror_run_completion(run_id, snapshot)
    except Exception:
        # mirror_run_completion already swallows; this catch is just
        # belt-and-braces for an import-time error from db_writethrough.
        pass


def _update_run_status(run_id: str, **kwargs) -> None:
    """Thread-safe update of RUN_STATUS fields, broadcasts on change, persists on completion."""
    with _run_status_lock:
        if run_id not in RUN_STATUS:
            RUN_STATUS[run_id] = {
                "phase": "", "score": 0, "completed": False,
                "result": None, "error": None,
            }
        RUN_STATUS[run_id].update(kwargs)
        snapshot = dict(RUN_STATUS[run_id])
    # Phase 2.2: mirror to Redis (best-effort; no-op when disabled).
    _mirror_run_status_to_redis(run_id, snapshot, operation="update")
    if any(k in kwargs for k in ("phase", "score", "completed", "error")):
        _ws_broadcast({
            "type": "status", "run_id": run_id,
            "phase": snapshot.get("phase", ""),
            "score": snapshot.get("score", 0),
            "completed": snapshot.get("completed", False),
            "error": snapshot.get("error"),
            "project": snapshot.get("project", ""),
            "target": snapshot.get("target", ""),
        })
    if kwargs.get("completed"):
        _persist_run_index(run_id, snapshot)


def _init_run_status(run_id: str, **kwargs) -> None:
    """Thread-safe initialization of a new run entry.

    Fields:
      phase                  str   — current lifecycle phase / last log message
      score                  float — best score achieved (0–10)
      completed              bool  — True once the run reaches a terminal state
      result                 dict | None — final result payload on success
      error                  str | None  — error message on failure
      _judge_primary_down    bool  — True if the primary judge LLM was unreachable
      manifest_status        Literal["ok","corrupted","missing","skipped"] | None
                             — "ok": manifest.json written and hashes verified       [Phase B: orchestration hook]
                             — "skipped": write attempted but failed (non-fatal)     [Phase B: orchestration hook]
                             — "corrupted": manifest written but hashes don't match  [Phase D: /runs/{id}/verify endpoint]
                             — "missing": manifest.json absent after run             [Phase D: /runs/{id}/verify endpoint]
                             — None: not yet attempted (run still in progress)
    """
    with _run_status_lock:
        RUN_STATUS[run_id] = {
            "phase": "queued", "score": 0, "completed": False,
            "result": None, "error": None, "_judge_primary_down": False,
            "manifest_status": None,
            **kwargs,
        }
        snapshot = dict(RUN_STATUS[run_id])
    # Phase 2.2: mirror to Redis (best-effort; no-op when disabled).
    _mirror_run_status_to_redis(run_id, snapshot, operation="init")
    try:
        from core.metrics import observe_run_started
        observe_run_started()
    except Exception:
        pass


# ── log writer ─────────────────────────────────────
def log(run_id: str, message: str) -> None:
    """Per-run log writer with file lock + WS broadcast + status update.

    Phase 2.3: when there's an active OTel span on the calling thread
    (ie. we're inside an LLM call, ssh_command, or anything wrapped by
    a span), the log line is attached as a span event with run_id +
    message attributes. Zero-cost when no span is active or OTel is
    disabled — span events on the no-op tracer return immediately.
    """
    ts = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] [{run_id}] {message}"
    print(line)

    log_path = Path(LOG_DIR) / f"{run_id}.log"
    try:
        with open(log_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(f"WARNING: could not write to log file: {e}")

    # Best-effort: attach the log line as a span event to whatever span
    # is active on this thread. ``trace.get_current_span()`` returns
    # OTel's INVALID_SPAN sentinel when there's no active span, and
    # ``add_event`` on that sentinel is a no-op — zero cost.
    try:
        from opentelemetry import trace as _otel_trace  # noqa: PLC0415
        _otel_trace.get_current_span().add_event(
            "orchestrator.log",
            attributes={"run_id": run_id or "", "message": message[:500]},
        )
    except Exception:
        pass

    _update_run_status(run_id, phase=message)
    _ws_broadcast({"type": "log", "run_id": run_id, "line": line, "phase": message})
