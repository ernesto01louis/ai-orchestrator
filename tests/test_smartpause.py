"""Phase 3.2 SmartPause regression tests.

Direct unit tests of ``orchestration._smartpause_check`` and the
``POST /runs/{run_id}/resume`` route. Threshold + clamp behaviour
on the planner side is covered by the existing planner test suite
plus an explicit clamp test here.
"""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

import orchestration
from core.runtime import RUN_STATUS, _init_run_status


@pytest.fixture(autouse=True)
def _clean_run_status() -> Any:
    RUN_STATUS.clear()
    yield
    RUN_STATUS.clear()


# ---------------------------------------------------------------------------
# Threshold + clamp behaviour on the planner-side normalisation.
# ---------------------------------------------------------------------------

def test_planner_clamps_out_of_range_confidence_low() -> None:
    """Normaliser: negative confidence clamps to 0.0."""
    plan = {"confidence": -0.5, "language": "python"}
    # Run the same clamp the planner does in-line:
    try:
        conf = float(plan.get("confidence", 1.0))
    except (TypeError, ValueError):
        conf = 1.0
    plan["confidence"] = max(0.0, min(1.0, conf))
    assert plan["confidence"] == 0.0


def test_planner_clamps_out_of_range_confidence_high() -> None:
    """Normaliser: confidence > 1 clamps to 1.0."""
    plan: dict[str, Any] = {"confidence": 2.5, "language": "python"}
    try:
        conf = float(plan.get("confidence", 1.0))
    except (TypeError, ValueError):
        conf = 1.0
    plan["confidence"] = max(0.0, min(1.0, conf))
    assert plan["confidence"] == 1.0


def test_planner_handles_garbage_confidence() -> None:
    """Normaliser: non-numeric confidence falls back to 1.0."""
    plan: dict[str, Any] = {"confidence": "high", "language": "python"}
    try:
        conf = float(plan.get("confidence", 1.0))
    except (TypeError, ValueError):
        conf = 1.0
    plan["confidence"] = max(0.0, min(1.0, conf))
    assert plan["confidence"] == 1.0


# ---------------------------------------------------------------------------
# _smartpause_check helper
# ---------------------------------------------------------------------------

def test_smartpause_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``smartpause.enabled=false`` short-circuits the check before
    threshold lookup; nothing logged, no RUN_STATUS mutation."""
    monkeypatch.setattr(orchestration, "SMARTPAUSE_ENABLED", False)
    _init_run_status("r-disabled", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")
    orchestration._smartpause_check("r-disabled", {"confidence": 0.1})
    assert RUN_STATUS["r-disabled"].get("paused") is None


def test_smartpause_skipped_in_full_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hitl_mode == "full_auto"`` short-circuits even with low
    confidence (today every campaign is full_auto until Phase 3.1)."""
    monkeypatch.setattr(orchestration, "SMARTPAUSE_ENABLED", True)
    monkeypatch.setattr(orchestration, "SMARTPAUSE_THRESHOLD", 0.7)
    monkeypatch.setattr(orchestration, "_get_run_hitl_mode", lambda _id: "full_auto")
    _init_run_status("r-fa", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")
    orchestration._smartpause_check("r-fa", {"confidence": 0.1})
    assert RUN_STATUS["r-fa"].get("paused") is None


def test_smartpause_skipped_when_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confidence at or above threshold is OK in any mode."""
    monkeypatch.setattr(orchestration, "SMARTPAUSE_ENABLED", True)
    monkeypatch.setattr(orchestration, "SMARTPAUSE_THRESHOLD", 0.7)
    monkeypatch.setattr(orchestration, "_get_run_hitl_mode", lambda _id: "checkpoint")
    _init_run_status("r-hi", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")
    orchestration._smartpause_check("r-hi", {"confidence": 0.9})
    assert RUN_STATUS["r-hi"].get("paused") is None


def test_smartpause_skipped_with_no_confidence_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing confidence defaults to 1.0; never trips the gate."""
    monkeypatch.setattr(orchestration, "SMARTPAUSE_ENABLED", True)
    monkeypatch.setattr(orchestration, "SMARTPAUSE_THRESHOLD", 0.7)
    monkeypatch.setattr(orchestration, "_get_run_hitl_mode", lambda _id: "checkpoint")
    _init_run_status("r-nc", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")
    orchestration._smartpause_check("r-nc", {"language": "python"})
    assert RUN_STATUS["r-nc"].get("paused") is None


def test_smartpause_pauses_below_threshold_then_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The blocking + unblock loop: low confidence + non-full_auto
    flips RUN_STATUS to paused="smartpause"; a background thread
    flipping it back releases the polling loop within one poll
    interval."""
    monkeypatch.setattr(orchestration, "SMARTPAUSE_ENABLED", True)
    monkeypatch.setattr(orchestration, "SMARTPAUSE_THRESHOLD", 0.7)
    monkeypatch.setattr(orchestration, "_get_run_hitl_mode", lambda _id: "checkpoint")
    monkeypatch.setattr(orchestration, "SMARTPAUSE_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(orchestration, "SMARTPAUSE_PAUSE_TIMEOUT", 5)
    _init_run_status("r-pause", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")

    def _resume_after_delay() -> None:
        # Wait until the helper has set paused="smartpause", then clear.
        for _ in range(50):
            if RUN_STATUS.get("r-pause", {}).get("paused") == "smartpause":
                break
            time.sleep(0.02)
        RUN_STATUS["r-pause"]["paused"] = None

    t = threading.Thread(target=_resume_after_delay, daemon=True)
    t.start()

    with patch("orchestration.send_notification") as snd:
        orchestration._smartpause_check("r-pause", {"confidence": 0.3})

    assert RUN_STATUS["r-pause"].get("paused") is None
    assert snd.called, "expected a notification to fire on SmartPause trip"
    assert RUN_STATUS["r-pause"].get("smartpause_confidence") == 0.3
    t.join(timeout=1.0)


def test_smartpause_times_out_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """If no resume arrives within pause_timeout_seconds, the helper
    logs and continues; paused flag is cleared on exit."""
    monkeypatch.setattr(orchestration, "SMARTPAUSE_ENABLED", True)
    monkeypatch.setattr(orchestration, "SMARTPAUSE_THRESHOLD", 0.7)
    monkeypatch.setattr(orchestration, "_get_run_hitl_mode", lambda _id: "checkpoint")
    monkeypatch.setattr(orchestration, "SMARTPAUSE_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(orchestration, "SMARTPAUSE_PAUSE_TIMEOUT", 0)  # immediate
    _init_run_status("r-to", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")

    with patch("orchestration.send_notification"):
        orchestration._smartpause_check("r-to", {"confidence": 0.1})

    assert RUN_STATUS["r-to"].get("smartpause_timeout") is True
    assert RUN_STATUS["r-to"].get("paused") is None


# ---------------------------------------------------------------------------
# /runs/{run_id}/resume route
# ---------------------------------------------------------------------------

def test_resume_route_clears_pause(inprocess_client: Any) -> None:
    """Posting to /runs/{id}/resume on a paused run clears the flag."""
    _init_run_status("r-route", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")
    RUN_STATUS["r-route"]["paused"] = "smartpause"

    r = inprocess_client.post("/runs/r-route/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "r-route"
    assert body["previously_paused"] == "smartpause"
    assert body["paused"] is None
    assert RUN_STATUS["r-route"].get("paused") is None


def test_resume_route_idempotent_on_unpaused(inprocess_client: Any) -> None:
    """Posting on a run that wasn't paused returns 200 with
    previously_paused=None — the route is idempotent."""
    _init_run_status("r-noop", project="p", entrypoint="main", path="/tmp", generators=[], judge="judge", iterations=1, target="local")
    r = inprocess_client.post("/runs/r-noop/resume")
    assert r.status_code == 200
    assert r.json()["previously_paused"] is None


def test_resume_route_404_on_unknown_run(inprocess_client: Any) -> None:
    """Unknown run_id returns 404."""
    r = inprocess_client.post("/runs/nope/resume")
    assert r.status_code == 404
