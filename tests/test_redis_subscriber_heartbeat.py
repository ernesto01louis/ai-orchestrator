"""Phase 2 hardening — Redis WS-broadcast subscriber heartbeat + watchdog.

Locks in the contract that the subscriber loop:
- ticks ``orchestrator_redis_subscriber_heartbeat_total{outcome="tick"}`` on every
  successful poll (whether or not a message was returned),
- increments ``...{outcome="error"}`` on any exception from ``get_message``,
- increments ``orchestrator_redis_subscriber_restarts_total`` after re-subscribing.

The loop itself is an infinite ``while True`` in ``core.runtime``; testing it
directly would hang. Instead we exercise the three observer helpers and prove
they target the right Prom series — the loop's call sites are reviewed by
``test_redis_subscriber_loop_calls_observers``.
"""
from __future__ import annotations

from prometheus_client import REGISTRY

from core import metrics


def _counter_value(name: str, **labels: str) -> float:
    """Read the current value of a labeled Prom counter from the default registry."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_observe_redis_subscriber_tick_increments_tick_label() -> None:
    name = "orchestrator_redis_subscriber_heartbeat_total"
    before = _counter_value(name, outcome="tick")
    metrics.observe_redis_subscriber_tick()
    metrics.observe_redis_subscriber_tick()
    after = _counter_value(name, outcome="tick")
    # >= rather than == because the real Redis subscriber daemon may be
    # running concurrently (started via app lifespan in other tests) and
    # pushing its own ticks between our reads.
    assert after >= before + 2


def test_observe_redis_subscriber_error_increments_error_label() -> None:
    name = "orchestrator_redis_subscriber_heartbeat_total"
    before = _counter_value(name, outcome="error")
    metrics.observe_redis_subscriber_error()
    after = _counter_value(name, outcome="error")
    assert after >= before + 1


def test_observe_redis_subscriber_restart_increments_restarts() -> None:
    name = "orchestrator_redis_subscriber_restarts_total"
    before = _counter_value(name)
    metrics.observe_redis_subscriber_restart()
    after = _counter_value(name)
    assert after >= before + 1


def test_redis_subscriber_loop_calls_observers() -> None:
    """Source-text guard: the subscriber loop in ``core/runtime.py``
    references the three Prom observers. Catches a future refactor
    that removes the heartbeat instrumentation by accident.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent.joinpath("core", "runtime.py").read_text()
    for fn in (
        "observe_redis_subscriber_tick",
        "observe_redis_subscriber_error",
        "observe_redis_subscriber_restart",
    ):
        assert fn in source, (
            f"core/runtime.py missing reference to {fn}() — the Redis "
            f"WS-broadcast subscriber loop must keep the heartbeat / "
            f"restart instrumentation."
        )
