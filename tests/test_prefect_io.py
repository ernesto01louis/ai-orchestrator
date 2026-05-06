"""Tests for prefect_io façade — mode switch, fallback, helper helpers."""
from unittest.mock import MagicMock, patch

import pytest

from prefect_io import (
    cancel_flow_run,
    pause_flow_run,
    resume_flow_run,
    submit_campaign,
    submit_orchestration,
)


@pytest.fixture
def in_process_config(monkeypatch):
    monkeypatch.setattr(
        "prefect_io._get_execution_mode",
        lambda: "in_process",
    )


@pytest.fixture
def deployment_config(monkeypatch):
    monkeypatch.setattr(
        "prefect_io._get_execution_mode",
        lambda: "deployment",
    )


def test_submit_orchestration_in_process_returns_null_flow_run_id(in_process_config):
    """In-process mode lets Prefect generate the real flow_run_id once the
    @flow runs; the on_running state hook copies it into RUN_STATUS. The
    immediate POST response therefore returns ``flow_run_id=None`` and
    callers poll /status/<run_id> for the real value.
    """
    with patch("prefect_io._healthcheck", return_value=True), \
         patch("prefect_io._spawn_daemon_thread") as spawn_mock:
        req = MagicMock()
        out = submit_orchestration(req, run_id="r1")
        assert out == {"run_id": "r1", "flow_run_id": None}
        spawn_mock.assert_called_once()


def test_submit_orchestration_falls_back_when_server_unreachable(
    in_process_config, caplog
):
    with patch("prefect_io._healthcheck", return_value=False), \
         patch("prefect_io._spawn_daemon_thread_fallback") as fallback_mock, \
         patch("prefect_io._notify_prefect_down") as notify_mock:
        req = MagicMock()
        out = submit_orchestration(req, run_id="r1")
        assert out["run_id"] == "r1"
        assert out["flow_run_id"] is None
        fallback_mock.assert_called_once()
        notify_mock.assert_called_once()


def test_submit_orchestration_deployment_mode_calls_run_deployment(
    deployment_config,
):
    with patch("prefect_io._healthcheck", return_value=True), \
         patch("prefect_io._run_deployment", return_value="frid-456") as deploy_mock, \
         patch("prefect_io._spawn_daemon_thread") as spawn_mock:
        req = MagicMock()
        out = submit_orchestration(req, run_id="r1")
        assert out == {"run_id": "r1", "flow_run_id": "frid-456"}
        deploy_mock.assert_called_once()
        spawn_mock.assert_not_called()


def test_submit_campaign_in_process_returns_null_flow_run_id(in_process_config):
    """Same contract as orchestration: in-process mode returns
    ``flow_run_id=None`` and the on_running hook fills CAMPAIGN_STATUS.
    """
    with patch("prefect_io._healthcheck", return_value=True), \
         patch("prefect_io._spawn_daemon_thread"):
        out = submit_campaign(campaign_id="camp-1")
        assert out == {"campaign_id": "camp-1", "flow_run_id": None}


def test_pause_flow_run_calls_prefect_api():
    with patch("prefect_io._set_flow_run_state") as state_mock:
        pause_flow_run("frid-1")
        state_mock.assert_called_once()
        args, kwargs = state_mock.call_args
        assert args[0] == "frid-1"
        assert kwargs.get("state_type") == "PAUSED" or args[1] == "PAUSED"


def test_resume_flow_run_calls_prefect_api():
    with patch("prefect_io._set_flow_run_state") as state_mock:
        resume_flow_run("frid-1")
        state_mock.assert_called_once()


def test_cancel_flow_run_calls_prefect_api():
    with patch("prefect_io._set_flow_run_state") as state_mock:
        cancel_flow_run("frid-1")
        state_mock.assert_called_once()
        args, kwargs = state_mock.call_args
        assert "CANCELL" in (kwargs.get("state_type") or args[1])


def test_pause_flow_run_with_no_flow_run_id_is_noop():
    with patch("prefect_io._set_flow_run_state") as state_mock:
        pause_flow_run(None)
        state_mock.assert_not_called()


def test_unknown_execution_mode_raises(monkeypatch):
    monkeypatch.setattr("prefect_io._get_execution_mode", lambda: "magic")
    with pytest.raises(ValueError, match="execution_mode"):
        submit_orchestration(MagicMock(), run_id="r1")


def test_healthcheck_timeout_is_treated_as_unreachable(in_process_config):
    import socket
    with patch("prefect_io._raw_healthcheck",
               side_effect=socket.timeout()), \
         patch("prefect_io._spawn_daemon_thread_fallback") as fallback_mock, \
         patch("prefect_io._notify_prefect_down"):
        out = submit_orchestration(MagicMock(), run_id="r1")
        assert out["flow_run_id"] is None
        fallback_mock.assert_called_once()
