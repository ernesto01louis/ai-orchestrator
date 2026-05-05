"""Prefect state hooks → RUN_STATUS + LlmCall buffer.

Wired up to flows/tasks via the @flow(on_running=, on_completion=, ...)
and @task(on_completion=, on_failure=) parameters. All hooks must NEVER
raise into Prefect — wrap every body in a try/except.
"""
from __future__ import annotations

import logging
from typing import Any

import prefect.runtime.task_run as _prefect_task_run

from core.llm_call_log import LLM_CALL_LOG, LlmCallRecord

logger = logging.getLogger(__name__)


def _update_run_status(run_id: str, **fields: Any) -> None:
    """Delegate to core.runtime._update_run_status which acquires the lock,
    mutates RUN_STATUS, broadcasts to /ws, and persists.

    Kept as a thin wrapper so hooks can be re-pointed (e.g. for tests)
    without touching every call site.
    """
    from core.runtime import _update_run_status as _update
    _update(run_id, **fields)


def _extract_run_id(flow_run: Any) -> str | None:
    params = getattr(flow_run, "parameters", None) or {}
    return params.get("run_id")


def _extract_campaign_id(flow_run: Any) -> str | None:
    params = getattr(flow_run, "parameters", None) or {}
    return params.get("campaign_id")


def _safe_emit_evidence(campaign_id: str) -> None:
    """Best-effort evidence bundle build for a (potentially cancelled) campaign."""
    try:
        from evidence.builder import build_bundle
        build_bundle(campaign_id)
    except Exception as e:
        logger.warning("Evidence emit failed for %s: %s", campaign_id, e)


# ---------------------------------------------------------------------------
# Flow hooks
# ---------------------------------------------------------------------------

def on_running(flow: Any, flow_run: Any, state: Any) -> None:
    try:
        real_flow_run_id = str(getattr(flow_run, "id", "") or "") or None
        run_id = _extract_run_id(flow_run)
        if run_id:
            updates: dict[str, Any] = {"phase": state.name}
            if real_flow_run_id:
                updates["flow_run_id"] = real_flow_run_id
            _update_run_status(run_id, **updates)

        if real_flow_run_id and getattr(flow, "name", "") == "campaign":
            campaign_id = _extract_campaign_id(flow_run)
            if campaign_id:
                from core.runtime import CAMPAIGN_STATUS, _campaign_status_lock
                with _campaign_status_lock:
                    if campaign_id in CAMPAIGN_STATUS:
                        CAMPAIGN_STATUS[campaign_id]["flow_run_id"] = real_flow_run_id
    except Exception as e:
        logger.warning("on_running hook error: %s", e)


def on_completion(flow: Any, flow_run: Any, state: Any) -> None:
    try:
        run_id = _extract_run_id(flow_run)
        if run_id:
            _update_run_status(run_id, phase=state.name, completed=True)
    except Exception as e:
        logger.warning("on_completion hook error: %s", e)


def on_failure(flow: Any, flow_run: Any, state: Any) -> None:
    try:
        run_id = _extract_run_id(flow_run)
        if run_id:
            err = getattr(state, "message", None) or "Failed"
            _update_run_status(run_id, phase=state.name,
                               completed=True, error=err)
    except Exception as e:
        logger.warning("on_failure hook error: %s", e)


def on_cancelled(flow: Any, flow_run: Any, state: Any) -> None:
    try:
        run_id = _extract_run_id(flow_run)
        if run_id:
            _update_run_status(run_id, phase=state.name,
                               completed=True, error="Cancelled")

        # Campaign flows: emit evidence so cancelled campaigns still bundle.
        if getattr(flow, "name", "") == "campaign":
            campaign_id = _extract_campaign_id(flow_run)
            if campaign_id:
                _safe_emit_evidence(campaign_id)
    except Exception as e:
        logger.warning("on_cancelled hook error: %s", e)


# ---------------------------------------------------------------------------
# Task hooks
# ---------------------------------------------------------------------------

def on_task_completion(task: Any, task_run: Any, state: Any) -> None:
    """Capture LlmCall record when an `llm-call`-tagged task completes."""
    try:
        tags = getattr(task, "tags", None) or set()
        if "llm-call" not in tags:
            return

        # Prefer the runtime context (populated by Prefect's TaskRunContext during
        # execution; task_run.parameters is a server-side model field that Prefect
        # 3.6.x does NOT populate in the locally-invoked hook context).
        # Fall back to task_run.parameters so unit tests that inject a MagicMock
        # with task_run.parameters still pass.
        params: dict[str, Any] = (
            _prefect_task_run.parameters
            or getattr(task_run, "parameters", None)
            or {}
        )
        run_id = params.get("run_id")
        if not run_id:
            return

        # Best-effort duration calc
        start = getattr(task_run, "start_time", None)
        end = getattr(task_run, "end_time", None)
        duration_ms = 0
        try:
            if start and end:
                duration_ms = int((end - start).total_seconds() * 1000)
        except Exception:
            duration_ms = 0

        # Extract model + messages + sampling from task params
        model = params.get("model", "unknown")
        rendered = params.get("messages") or params.get("prompt") or []
        if isinstance(rendered, str):
            rendered = [{"role": "user", "content": rendered}]
        sampling = params.get("options") or params.get("sampling") or {}

        # Try to extract response_tokens from result
        response_tokens = 0
        try:
            result = state.result(raise_on_failure=False) if callable(
                getattr(state, "result", None)
            ) else None
            # LlmResponse exposes .eval_count; legacy dict has "eval_count" key.
            candidate = getattr(result, "eval_count", None)
            if candidate is None and isinstance(result, dict):
                candidate = result.get("eval_count") or result.get("response_tokens")
            response_tokens = int(candidate or 0)
        except Exception:
            response_tokens = 0

        LLM_CALL_LOG.append(LlmCallRecord(
            run_id=run_id,
            model=model,
            rendered_messages=rendered if isinstance(rendered, list) else [],
            sampling=sampling,
            response_tokens=int(response_tokens),
            duration_ms=int(duration_ms),
        ))
    except Exception as e:
        logger.warning("on_task_completion hook error: %s", e)
