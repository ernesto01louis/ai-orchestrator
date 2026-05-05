"""Façade between FastAPI / orchestration code and Prefect 3.

Public API:
    submit_orchestration(req, run_id) -> {"run_id", "flow_run_id"}
    submit_campaign(campaign_id) -> {"campaign_id", "flow_run_id"}
    pause_flow_run(flow_run_id) -> None
    resume_flow_run(flow_run_id) -> None
    cancel_flow_run(flow_run_id) -> None

Branches on config.prefect.execution_mode ("in_process" | "deployment").
Falls back to daemon-thread spawn (no Prefect tracking) when the server is
unreachable, after logging a WARNING and firing a Gotify notification.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from core.config import CONFIG

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _get_execution_mode() -> str:
    """Return "in_process" (default) or "deployment"."""
    val = CONFIG.get("prefect", {}).get("execution_mode", "in_process")
    return str(val)


def _get_api_url() -> str:
    val = CONFIG.get("prefect", {}).get(
        "api_url", "http://127.0.0.1:4200/api"
    )
    return str(val)


def _get_work_pool() -> str:
    val = CONFIG.get("prefect", {}).get("work_pool", "orchestrator-pool")
    return str(val)


# ---------------------------------------------------------------------------
# Server reachability
# ---------------------------------------------------------------------------

def _raw_healthcheck() -> bool:
    """Probe the Prefect server's /api/health endpoint.

    Separated from `_healthcheck` so tests can inject failures via
    ``socket.timeout`` etc. without touching the high-level retry path.
    """
    import httpx
    url = _get_api_url().rstrip("/") + "/health"
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url)
            return r.status_code == 200
    except (httpx.HTTPError, socket.timeout, OSError):
        return False


def _healthcheck() -> bool:
    """Defense-in-depth wrapper around _raw_healthcheck.

    `_raw_healthcheck` already returns False on transport errors; this catch
    handles unexpected programmer errors (AttributeError on missing config,
    etc.) so a misconfigured server URL never breaks submission.
    """
    try:
        return _raw_healthcheck()
    except Exception:
        return False


def _notify_prefect_down() -> None:
    """Best-effort notification; safe to call from any context."""
    try:
        from notifications.send import send_notification
        send_notification(  # type: ignore[no-untyped-call]
            title="Prefect server unreachable",
            message="Falling back to daemon-thread spawn for new runs.",
            priority=5,
        )
    except Exception as e:
        logger.warning("Notification failed: %s", e)


# ---------------------------------------------------------------------------
# Flow run id allocation + state setting
# ---------------------------------------------------------------------------

def _set_flow_run_state(flow_run_id: str, state_type: str) -> None:
    """Best-effort state transition via Prefect REST API.

    state_type: "PAUSED" | "RUNNING" | "CANCELLING".
    """
    import httpx
    url = _get_api_url().rstrip("/") + f"/flow_runs/{flow_run_id}/set_state"
    payload = {"state": {"type": state_type}}
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(url, json=payload)
    except (httpx.HTTPError, OSError) as e:
        logger.warning(
            "Failed to set flow_run %s -> %s: %s", flow_run_id, state_type, e
        )


def _run_deployment(deployment_name: str, parameters: dict[str, Any]) -> str:
    """Submit a flow run via Prefect deployment; return flow_run_id.

    `prefect.deployments.run_deployment` returns FlowRun synchronously when
    called from a sync context, or a Coroutine[FlowRun] when called from an
    async context. We're only ever called from sync code; if a coroutine
    comes back we drive it with asyncio.run.

    NOTE: must NOT be called from inside a running event loop. The FastAPI
    handlers that submit flows are sync, so this is safe in current usage.
    """
    import asyncio
    from prefect.deployments import run_deployment as _rd
    # timeout=0 means submit-and-return-immediately, do NOT block waiting
    # for the run to finish.
    result = _rd(name=deployment_name, parameters=parameters, timeout=0)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return str(result.id)


# ---------------------------------------------------------------------------
# Spawners
# ---------------------------------------------------------------------------

def _spawn_daemon_thread(
    flow_callable: Any, args: tuple[Any, ...]
) -> None:
    """In-process mode: invoke flow_callable in a daemon thread.

    Calling the flow callable (NOT .fn) keeps Prefect's state tracking,
    hooks, and retries active; the daemon thread is just to free the
    FastAPI request handler. Prefect generates the real flow_run_id when
    the flow starts; the on_running state hook copies it into RUN_STATUS /
    CAMPAIGN_STATUS so pause/resume/cancel endpoints can target it.
    """
    threading.Thread(target=flow_callable, args=args, daemon=True).start()


def _spawn_daemon_thread_fallback(
    flow_callable: Any, args: tuple[Any, ...]
) -> None:
    """Server-down fallback: invoke `.fn` (raw Python) in a daemon thread.

    No Prefect tracking, no hooks, no retries. Inline `_update_run_status`
    calls in run_orchestration / run_campaign keep the WebSocket UI alive.
    """
    raw_fn = getattr(flow_callable, "fn", flow_callable)
    threading.Thread(target=raw_fn, args=args, daemon=True).start()


# ---------------------------------------------------------------------------
# Public submission helpers
# ---------------------------------------------------------------------------

def submit_orchestration(req: Any, run_id: str) -> dict[str, str | None]:
    """Submit a single orchestrate flow run.

    Returns {"run_id": ..., "flow_run_id": ... | None}. flow_run_id is None
    when the server is unreachable and we fell back to the daemon-thread path.
    """
    mode = _get_execution_mode()
    if mode not in ("in_process", "deployment"):
        raise ValueError(f"Unknown prefect.execution_mode: {mode!r}")

    if not _healthcheck():
        logger.warning("Prefect server unreachable; using daemon-thread fallback")
        _notify_prefect_down()
        from orchestration import run_orchestration
        _spawn_daemon_thread_fallback(run_orchestration, (req, run_id))
        return {"run_id": run_id, "flow_run_id": None}

    if mode == "deployment":
        flow_run_id = _run_deployment(
            "orchestrate", {"req": req, "run_id": run_id}
        )
        return {"run_id": run_id, "flow_run_id": flow_run_id}

    # in_process — Prefect generates the real flow_run_id on its own when the
    # @flow runs. The on_running state hook captures it into RUN_STATUS so
    # pause/resume/cancel can target it. We can't return it synchronously
    # because the flow hasn't started yet; callers poll /status/<run_id>.
    from orchestration import run_orchestration
    _spawn_daemon_thread(run_orchestration, (req, run_id))
    return {"run_id": run_id, "flow_run_id": None}


def submit_campaign(campaign_id: str) -> dict[str, str | None]:
    """Submit a campaign flow run."""
    mode = _get_execution_mode()
    if mode not in ("in_process", "deployment"):
        raise ValueError(f"Unknown prefect.execution_mode: {mode!r}")

    if not _healthcheck():
        logger.warning("Prefect server unreachable; using daemon-thread fallback")
        _notify_prefect_down()
        from orchestration.campaign import run_campaign
        _spawn_daemon_thread_fallback(run_campaign, (campaign_id,))
        return {"campaign_id": campaign_id, "flow_run_id": None}

    if mode == "deployment":
        flow_run_id = _run_deployment(
            "campaign", {"campaign_id": campaign_id}
        )
        return {"campaign_id": campaign_id, "flow_run_id": flow_run_id}

    # in_process — Prefect generates the real flow_run_id; the on_running
    # state hook copies it into CAMPAIGN_STATUS for pause/resume/cancel.
    from orchestration.campaign import run_campaign
    _spawn_daemon_thread(run_campaign, (campaign_id,))
    return {"campaign_id": campaign_id, "flow_run_id": None}


# ---------------------------------------------------------------------------
# Pause / resume / cancel
# ---------------------------------------------------------------------------

def pause_flow_run(flow_run_id: str | None) -> None:
    if not flow_run_id:
        return
    _set_flow_run_state(flow_run_id, "PAUSED")


def resume_flow_run(flow_run_id: str | None) -> None:
    if not flow_run_id:
        return
    _set_flow_run_state(flow_run_id, "RUNNING")


def cancel_flow_run(flow_run_id: str | None) -> None:
    if not flow_run_id:
        return
    _set_flow_run_state(flow_run_id, "CANCELLING")
