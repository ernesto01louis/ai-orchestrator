"""Phase 3.1 HITL regression tests.

Covers:
  * ``_mode_pauses_at_phase`` truth table (5 modes × 7 phases).
  * ``get_run_hitl_mode`` lookup against a mocked campaigns.json.
  * ``hitl_checkpoint`` inert path (full_auto) and active path
    (checkpoint mode → notify + wait + resume).
  * ``POST /runs/{id}/intervene`` route shape (200 / 400 / 404).
"""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

import core.hitl as hitl
from core.runtime import RUN_STATUS, _init_run_status


@pytest.fixture(autouse=True)
def _clean() -> Any:
    RUN_STATUS.clear()
    hitl.INTERVENTION_QUEUE.clear()
    yield
    RUN_STATUS.clear()
    hitl.INTERVENTION_QUEUE.clear()


# ---------------------------------------------------------------------------
# _mode_pauses_at_phase truth table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,phase,expected", [
    # full_auto never pauses
    ("full_auto", "post_planner", False),
    ("full_auto", "post_llm", False),
    ("full_auto", "gate_denied", False),
    # gate_only pauses only on gate_denied
    ("gate_only", "post_planner", False),
    ("gate_only", "gate_denied", True),
    ("gate_only", "post_llm", False),
    # checkpoint pauses at boundaries + gate_denied, not per-LLM
    ("checkpoint", "post_planner", True),
    ("checkpoint", "post_generator", True),
    ("checkpoint", "post_judge", True),
    ("checkpoint", "post_optimizer", True),
    ("checkpoint", "gate_denied", True),
    ("checkpoint", "post_llm", False),
    ("checkpoint", "pre_llm", False),
    # step_by_step adds post_llm
    ("step_by_step", "post_planner", True),
    ("step_by_step", "post_llm", True),
    ("step_by_step", "pre_llm", False),
    ("step_by_step", "gate_denied", True),
    # co_pilot adds pre_llm (NOT post_llm)
    ("co_pilot", "post_planner", True),
    ("co_pilot", "pre_llm", True),
    ("co_pilot", "post_llm", False),
    ("co_pilot", "gate_denied", True),
])
def test_mode_pauses_at_phase_truth_table(mode: str, phase: str, expected: bool) -> None:
    assert hitl._mode_pauses_at_phase(mode, phase) is expected


# ---------------------------------------------------------------------------
# get_run_hitl_mode lookup
# ---------------------------------------------------------------------------

def test_get_run_hitl_mode_no_campaign_returns_default() -> None:
    """Single-shot run with no parent campaign → HITL_DEFAULT_MODE."""
    with patch("memory_pkg.load_campaigns", return_value={}):
        assert hitl.get_run_hitl_mode("orphan-run") == "full_auto"


def test_get_run_hitl_mode_finds_campaign_template_field() -> None:
    """Campaign with hitl_mode set → returns it."""
    fake_campaigns = {
        "c-1": {
            "id": "c-1",
            "template": {"hitl_mode": "checkpoint", "project_name": "p"},
            "runs": [{"run_id": "r-1", "status": "running"}],
        },
    }
    with patch("memory_pkg.load_campaigns", return_value=fake_campaigns):
        assert hitl.get_run_hitl_mode("r-1") == "checkpoint"


def test_get_run_hitl_mode_falls_back_when_field_missing() -> None:
    """Campaign without hitl_mode in template → HITL_DEFAULT_MODE."""
    fake_campaigns = {
        "c-1": {
            "id": "c-1",
            "template": {"project_name": "p"},  # no hitl_mode
            "runs": [{"run_id": "r-2"}],
        },
    }
    with patch("memory_pkg.load_campaigns", return_value=fake_campaigns):
        assert hitl.get_run_hitl_mode("r-2") == "full_auto"


def test_get_run_hitl_mode_rejects_invalid_mode() -> None:
    """Garbage hitl_mode in campaign → HITL_DEFAULT_MODE (defensive)."""
    fake_campaigns = {
        "c-1": {
            "template": {"hitl_mode": "wat"},
            "runs": [{"run_id": "r-3"}],
        },
    }
    with patch("memory_pkg.load_campaigns", return_value=fake_campaigns):
        assert hitl.get_run_hitl_mode("r-3") == "full_auto"


# ---------------------------------------------------------------------------
# hitl_checkpoint
# ---------------------------------------------------------------------------

def test_hitl_checkpoint_inert_in_full_auto() -> None:
    """full_auto → returns None immediately, no RUN_STATUS mutation,
    no notification."""
    _init_run_status("r-fa")
    with patch("core.hitl.get_run_hitl_mode", return_value="full_auto"), \
         patch("notifications.send.notify_intervention") as notify:
        result = hitl.hitl_checkpoint("r-fa", "post_planner")
    assert result is None
    assert notify.call_count == 0
    assert RUN_STATUS["r-fa"].get("paused") is None


def test_hitl_checkpoint_pauses_in_checkpoint_mode_then_resumes() -> None:
    """checkpoint mode + boundary phase → pauses, notifies, blocks
    until intervention arrives."""
    _init_run_status("r-cp")

    def _resume_after_delay() -> None:
        time.sleep(0.1)
        hitl.post_intervention("r-cp", {"action": "approve"})

    threading.Thread(target=_resume_after_delay, daemon=True).start()

    with patch("core.hitl.get_run_hitl_mode", return_value="checkpoint"), \
         patch("notifications.send.notify_intervention") as notify:
        result = hitl.hitl_checkpoint("r-cp", "post_planner", timeout_seconds=5.0)

    assert notify.call_count == 1
    assert result is not None
    assert result["action"] == "approve"
    assert RUN_STATUS["r-cp"].get("paused") is None
    assert RUN_STATUS["r-cp"].get("last_intervention") == "approve"


def test_hitl_checkpoint_times_out() -> None:
    """No intervention within timeout → returns None, hitl_timeout=True."""
    _init_run_status("r-to")
    with patch("core.hitl.get_run_hitl_mode", return_value="checkpoint"), \
         patch("notifications.send.notify_intervention"):
        result = hitl.hitl_checkpoint("r-to", "post_planner", timeout_seconds=0.1)
    assert result is None
    assert RUN_STATUS["r-to"].get("hitl_timeout") is True
    assert RUN_STATUS["r-to"].get("paused") is None


def test_hitl_checkpoint_co_pilot_threads_edit_payload() -> None:
    """co_pilot + pre_llm + action=edit returns the prompt override
    so the LLM-call wrap can swap user_prompt."""
    _init_run_status("r-cp2")

    def _resume_after_delay() -> None:
        time.sleep(0.1)
        hitl.post_intervention(
            "r-cp2", {"action": "edit", "prompt": "rewritten"},
        )

    threading.Thread(target=_resume_after_delay, daemon=True).start()

    with patch("core.hitl.get_run_hitl_mode", return_value="co_pilot"), \
         patch("notifications.send.notify_intervention"):
        result = hitl.hitl_checkpoint("r-cp2", "pre_llm", timeout_seconds=5.0)

    assert result is not None
    assert result.get("action") == "edit"
    assert result.get("prompt") == "rewritten"


# ---------------------------------------------------------------------------
# /runs/{id}/intervene route
# ---------------------------------------------------------------------------

def test_intervene_route_404_on_unknown(inprocess_client: Any) -> None:
    r = inprocess_client.post("/runs/nope/intervene", json={"action": "approve"})
    assert r.status_code == 404


def test_intervene_route_400_on_invalid_action(inprocess_client: Any) -> None:
    _init_run_status("r-bad")
    r = inprocess_client.post("/runs/r-bad/intervene", json={"action": "wat"})
    assert r.status_code == 400


def test_intervene_route_400_on_edit_without_prompt(inprocess_client: Any) -> None:
    _init_run_status("r-no-prompt")
    r = inprocess_client.post("/runs/r-no-prompt/intervene", json={"action": "edit"})
    assert r.status_code == 400


def test_intervene_route_queues_payload(inprocess_client: Any) -> None:
    """Approve action drops payload onto the per-run queue."""
    _init_run_status("r-q")
    r = inprocess_client.post("/runs/r-q/intervene", json={"action": "approve"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"run_id": "r-q", "action": "approve", "queued": True}
    # Drain the queue and verify the payload landed.
    payload = hitl.wait_for_intervention("r-q", timeout_seconds=1.0)
    assert payload == {"action": "approve"}


def test_intervene_route_clears_pause_flag(inprocess_client: Any) -> None:
    """Posting also clears RUN_STATUS[run_id]["paused"]."""
    _init_run_status("r-clear")
    RUN_STATUS["r-clear"]["paused"] = "hitl:post_planner"
    r = inprocess_client.post("/runs/r-clear/intervene", json={"action": "approve"})
    assert r.status_code == 200
    assert RUN_STATUS["r-clear"].get("paused") is None


def test_intervene_route_threads_edit_prompt(inprocess_client: Any) -> None:
    """action=edit + prompt drops the prompt onto the queue."""
    _init_run_status("r-e")
    r = inprocess_client.post(
        "/runs/r-e/intervene",
        json={"action": "edit", "prompt": "different prompt"},
    )
    assert r.status_code == 200
    payload = hitl.wait_for_intervention("r-e", timeout_seconds=1.0)
    assert payload == {"action": "edit", "prompt": "different prompt"}
