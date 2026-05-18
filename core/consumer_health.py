"""Phase 3.6 consumer-health daemon.

Polls each registered consumer's ``GET /healthz`` on an interval and
stamps ``last_health`` into ``consumers.json`` so ``GET /consumers``
shows whether a registered consumer is actually reachable.

Ships dormant: ``consumers.health_poll_seconds = 0`` (the default)
means ``start_consumer_health_daemon`` is a no-op — a fresh deploy
makes no outbound calls. An operator opts in by setting a positive
poll interval in ``config.json``.

Pattern mirrors ``core.sky.start_idle_stop_daemon`` — idempotent, runs
in a daemon thread, every failure is logged and swallowed.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import requests

from core.config import CONSUMERS_HEALTH_POLL_SECONDS
from memory_pkg import load_consumers, save_consumers

_logger = logging.getLogger(__name__)

_health_daemon_thread: threading.Thread | None = None
_health_daemon_lock = threading.Lock()
_health_daemon_stop = threading.Event()

# Per-probe timeout — short, so one wedged consumer can't stall the
# whole poll pass.
_PROBE_TIMEOUT_SECONDS = 5.0


def _probe_one(base_url: str) -> str:
    """Return a health status string for one consumer's /healthz."""
    try:
        resp = requests.get(
            f"{base_url}/healthz", timeout=_PROBE_TIMEOUT_SECONDS
        )
        return "ok" if resp.status_code < 400 else f"http_{resp.status_code}"
    except requests.RequestException:
        return "unreachable"


def health_poll_pass() -> int:
    """Probe every registered consumer once; persist results.

    Returns the number of consumers probed. Safe to call directly (the
    daemon loop calls it, tests can too).
    """
    registry = load_consumers()
    if not registry:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    for record in registry.values():
        base_url = record.get("base_url") or ""
        status = _probe_one(base_url) if base_url else "unknown"
        record["last_health"] = {"status": status, "checked_at": now}
    save_consumers(registry)
    return len(registry)


def start_consumer_health_daemon() -> None:
    """Start the consumer-health poll daemon.

    Idempotent. No-op when ``consumers.health_poll_seconds <= 0`` — the
    daemon never starts, so a dormant deploy issues no outbound probes.
    """
    interval = CONSUMERS_HEALTH_POLL_SECONDS
    if interval <= 0:
        return

    global _health_daemon_thread
    with _health_daemon_lock:
        if (
            _health_daemon_thread is not None
            and _health_daemon_thread.is_alive()
        ):
            return
        _health_daemon_stop.clear()

        def _loop() -> None:
            _logger.info(
                "consumer_health_daemon_started poll_interval=%ss", interval
            )
            while not _health_daemon_stop.wait(interval):
                try:
                    health_poll_pass()
                except Exception as exc:  # pragma: no cover — defensive
                    _logger.warning(
                        "consumer_health_pass_failed error=%s", exc
                    )

        thread = threading.Thread(
            target=_loop,
            name="consumer-health-daemon",
            daemon=True,
        )
        _health_daemon_thread = thread
        thread.start()


def stop_consumer_health_daemon() -> None:
    """Signal the daemon to exit at the next poll boundary."""
    _health_daemon_stop.set()
