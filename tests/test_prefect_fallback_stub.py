"""Phase 2 hardening — Prefect server-down fallback synthesises a stub
``LlmCallRecord`` so evidence bundles from fallback runs are non-empty.

Contract:
- When ``_spawn_daemon_thread_fallback`` is called with a ``run_id``,
  a single ``LlmCallRecord`` is appended to ``LLM_CALL_LOG`` after the
  underlying ``raw_fn`` returns (success OR exception).
- The stub carries ``call_id="fallback-<uuid>"``,
  ``agent_role="fallback"``, ``model_digest="unavailable"``,
  ``server_url="prefect-server-down"`` so verifiers can recognise the
  degradation.
- Stub-synth failure must never propagate up the thread.
- Campaign fallback (no ``run_id`` at spawn time) skips the stub.
"""
from __future__ import annotations

import threading
import time

import prefect_io
from core.llm_call_log import LLM_CALL_LOG


def _drain_eventually(run_id: str, timeout: float = 1.0) -> list:
    """Wait up to ``timeout`` for the fallback daemon thread to finish
    and the stub to land on LLM_CALL_LOG. Returns drained records."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = LLM_CALL_LOG.drain(run_id)
        if records:
            return records
        time.sleep(0.02)
    return LLM_CALL_LOG.drain(run_id)


def test_fallback_synthesises_stub_llm_call_on_success() -> None:
    """Happy path: raw_fn returns normally → stub appended."""
    run_id = "fb-success"
    done = threading.Event()

    def fake_fn(req, rid):
        time.sleep(0.01)
        done.set()

    prefect_io._spawn_daemon_thread_fallback(fake_fn, ("req", run_id), run_id=run_id)
    done.wait(timeout=1.0)

    records = _drain_eventually(run_id)
    assert len(records) == 1
    rec = records[0]
    assert rec.run_id == run_id
    assert rec.agent_role == "fallback"
    assert rec.model_digest == "unavailable"
    assert rec.server_url == "prefect-server-down"
    assert rec.call_id.startswith("fallback-")
    assert rec.started_at is not None
    assert "fallback" in rec.response_text.lower()


def test_fallback_stub_appended_even_when_raw_fn_raises() -> None:
    """Failure path: raw_fn raises → stub still appended via try/finally."""
    run_id = "fb-error"
    done = threading.Event()

    def boom(req, rid):
        try:
            raise RuntimeError("simulated boom inside raw_fn")
        finally:
            done.set()

    prefect_io._spawn_daemon_thread_fallback(boom, ("req", run_id), run_id=run_id)
    done.wait(timeout=1.0)

    records = _drain_eventually(run_id)
    assert len(records) == 1
    assert records[0].call_id.startswith("fallback-")


def test_fallback_without_run_id_skips_stub() -> None:
    """Campaign-level fallback (no run_id at spawn time) skips the stub."""
    done = threading.Event()

    def fake_campaign_fn(campaign_id):
        time.sleep(0.01)
        done.set()

    prefect_io._spawn_daemon_thread_fallback(
        fake_campaign_fn, ("campaign-xyz",),
    )
    done.wait(timeout=1.0)
    # Give the daemon thread a beat to truly finish before draining.
    time.sleep(0.05)

    # Nothing should be in LLM_CALL_LOG under campaign-xyz (no run_id passed).
    assert LLM_CALL_LOG.drain("campaign-xyz") == []
    # And nothing under common "fallback" sentinels either.
    assert LLM_CALL_LOG.drain("") == []


def test_fallback_stub_resilient_to_logger_failure(monkeypatch) -> None:
    """If LLM_CALL_LOG.append blows up, the daemon thread must still
    complete without raising. (try/except inside _append_fallback_stub
    keeps the thread alive.)"""
    run_id = "fb-broken-logger"
    done = threading.Event()

    def fake_fn(req, rid):
        done.set()

    # Force append to raise.
    def explode(_record):
        raise RuntimeError("simulated logger failure")

    monkeypatch.setattr(LLM_CALL_LOG, "append", explode)

    # Should not raise out of the spawner OR the thread.
    prefect_io._spawn_daemon_thread_fallback(fake_fn, ("req", run_id), run_id=run_id)
    done.wait(timeout=1.0)
    time.sleep(0.05)  # let the daemon finish its finally-block

    # If we got here without an exception, the resilience contract holds.
    # Nothing in LLM_CALL_LOG because append blew up.
    assert LLM_CALL_LOG.drain(run_id) == []
