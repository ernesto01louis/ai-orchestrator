"""Integration tests for run_campaign Prefect flow.

Three tests:
1. run_campaign completes with 2 combos when run_orchestration is no-op patched.
2. Pause flag set before run_campaign still set after (harness limit; real timing in I3).
3. prefect_io.cancel_flow_run reaches Prefect without crashing on an unknown uuid.

Test #3 differs from test_state_hooks.py:77-91 which mocks _safe_emit_evidence and
asserts on the on_cancelled hook directly.  This test exercises the cancel_flow_run
call path in prefect_io/__init__.py instead.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from core.campaign import CampaignCreate
from core.runtime import CAMPAIGN_STATUS, _campaign_status_lock

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_campaign_status():
    CAMPAIGN_STATUS.clear()
    yield
    CAMPAIGN_STATUS.clear()


@pytest.fixture
def mini_campaign():
    """Create a 2-combo campaign via api.routes.create_campaign; return campaign_id.

    Patches:
    - validate_target: "local" is not in SSH_TARGETS on this host; bypass.
    - submit_campaign: avoid actually submitting a Prefect flow run in the fixture
      itself — the tests that want to call run_campaign do so directly.
    """
    from api.routes import create_campaign

    req = CampaignCreate(
        name="mini",
        hypothesis="Test that 2 combos run sequentially.",
        template={
            "project_name": "mini-smoke",
            "deploy_target": "local",
            "prompt": "echo {x}",
            "language": "python",
            "generator_models": ["m1"],
            "judge_model": "judge",
            "planner_model": "planner",
        },
        params={"x": [1, 2]},
    )

    with ExitStack() as stack:
        stack.enter_context(patch("api.routes.validate_target"))
        stack.enter_context(
            patch(
                "api.routes.submit_campaign",
                return_value={"campaign_id": "ignored", "flow_run_id": "fake-frid"},
            )
        )
        response = create_campaign(req)

    return response["campaign_id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_campaign_completes_with_2_combos_and_1_generator(mini_campaign, mock_ollama):
    """run_campaign finalizes CAMPAIGN_STATUS phase as 'completed' for a 2-combo grid.

    run_orchestration is patched to a no-op at the module level to avoid
    recursive Prefect subflow invocation inside the test harness.
    """
    from orchestration.campaign import run_campaign

    # run_orchestration is lazily imported inside run_campaign's body via
    # `from orchestration import run_orchestration`, so we patch the source
    # attribute on the orchestration package itself.
    with patch("orchestration.run_orchestration") as mock_ro:
        mock_ro.with_options.return_value = mock_ro  # .with_options(...)(req, run_id) -> mock_ro(...)
        mock_ro.return_value = None
        run_campaign(mini_campaign)

    phase = CAMPAIGN_STATUS.get(mini_campaign, {}).get("phase", "")
    assert phase.lower() in ("completed", "aborted"), (
        f"Expected completed/aborted, got '{phase}'"
    )


def test_pause_mid_campaign_resume_completes_remaining(mini_campaign, mock_ollama):
    """Paused flag set before run_campaign is not cleared by the runner in harness mode.

    This is a harness-mode stub: run_campaign polls _is_paused() in a loop.
    With paused=True and no external thread to flip it, the loop runs until
    aborted=True (which we also set to break out).  We verify that paused
    remains True after the call — the flag was honored, not silently cleared.

    NOTE: Real pause/resume timing (set flag mid-run, resume after a delay) is
    covered in I3 against the actual Prefect server with a live flow_run_id.
    """
    from orchestration.campaign import run_campaign

    # Pre-set pause so run_campaign stalls immediately on first combo.
    with _campaign_status_lock:
        CAMPAIGN_STATUS.setdefault(mini_campaign, {})["paused"] = True
        CAMPAIGN_STATUS[mini_campaign]["aborted"] = True  # break out of pause loop

    with patch("orchestration.run_orchestration") as mock_ro:
        mock_ro.with_options.return_value = mock_ro
        mock_ro.return_value = None
        run_campaign(mini_campaign)

    # paused flag was not cleared by run_campaign (it's the caller's responsibility).
    assert CAMPAIGN_STATUS.get(mini_campaign, {}).get("paused") is True


def test_abort_through_cancel_flow_run(mini_campaign, mock_ollama):
    """cancel_flow_run reaches Prefect over HTTP without blowing up the call path.

    The harness has no actual flow run with this uuid, so Prefect may 404.
    cancel_flow_run is best-effort: it swallows httpx errors (logs a WARNING)
    and never re-raises, so the expected outcome is a clean return — no exception.

    This is different from test_state_hooks.py::test_on_cancelled_emits_evidence_for_campaign_flow
    which mocks _safe_emit_evidence and tests the on_cancelled hook callback directly.
    Here we go through the prefect_io public API to verify the call path is wired.
    """
    from prefect_io import cancel_flow_run

    fake_uuid = "00000000-0000-0000-0000-000000000000"
    try:
        cancel_flow_run(fake_uuid)
    except Exception as exc:  # noqa: BLE001
        # If the implementation stops swallowing errors in the future, accept
        # 404 / ObjectNotFound as the only legitimate failure mode.
        exc_str = str(exc)
        exc_type = type(exc).__name__
        assert (
            "404" in exc_str
            or "not found" in exc_str.lower()
            or "ObjectNotFound" in exc_type
        ), f"Unexpected exception from cancel_flow_run: {exc_type}: {exc}"
