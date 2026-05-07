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
            try:
                from core.metrics import observe_run_succeeded
                observe_run_succeeded()
            except Exception:
                pass
    except Exception as e:
        logger.warning("on_completion hook error: %s", e)


def on_failure(flow: Any, flow_run: Any, state: Any) -> None:
    try:
        run_id = _extract_run_id(flow_run)
        if run_id:
            err = getattr(state, "message", None) or "Failed"
            _update_run_status(run_id, phase=state.name,
                               completed=True, error=err)
            try:
                from core.metrics import observe_run_failed, observe_run_timed_out
                state_name: str = getattr(state, "name", "") or ""
                if state_name.lower() == "timedout":
                    observe_run_timed_out()
                else:
                    observe_run_failed()
            except Exception:
                pass
    except Exception as e:
        logger.warning("on_failure hook error: %s", e)


def on_cancelled(flow: Any, flow_run: Any, state: Any) -> None:
    try:
        run_id = _extract_run_id(flow_run)
        if run_id:
            _update_run_status(run_id, phase=state.name,
                               completed=True, error="Cancelled")
            try:
                from core.metrics import observe_run_aborted
                observe_run_aborted()
            except Exception:
                pass

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

        # Best-effort duration calc + start_time pass-through
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

        # Phase J β citation-grade fields ---
        call_id = str(getattr(task_run, "id", "") or "")
        server_url = str(params.get("url", "") or "")
        agent_role = str(params.get("agent_role", "") or "")
        started_at = start if hasattr(start, "tzinfo") else None

        # Pull eval_count / digest / size / response_text from the LlmResponse
        # envelope. Tolerate dicts (legacy callers / fixtures) and bare values.
        response_tokens = 0
        envelope: dict[str, Any] = {}
        try:
            result = state.result(raise_on_failure=False) if callable(
                getattr(state, "result", None)
            ) else None
            env_attr = getattr(result, "envelope", None)
            if isinstance(env_attr, dict):
                envelope = env_attr
            elif isinstance(result, dict):
                envelope = result
            candidate = getattr(result, "eval_count", None)
            if candidate is None:
                candidate = envelope.get("eval_count") or envelope.get("response_tokens")
            response_tokens = int(candidate or 0)
        except Exception:
            response_tokens = 0
            envelope = {}

        model_digest = str(envelope.get("_orchestrator_digest", "") or "")
        try:
            model_size_bytes = int(envelope.get("_orchestrator_size_bytes", 0) or 0)
        except (TypeError, ValueError):
            model_size_bytes = 0
        response_text = str(envelope.get("_orchestrator_response_text", "") or "")
        # If caller didn't pass agent_role at the call site, fall back to the
        # value the production code stamped into the envelope.
        if not agent_role:
            agent_role = str(envelope.get("_orchestrator_agent_role", "") or "")

        record = LlmCallRecord(
            run_id=run_id,
            model=model,
            rendered_messages=rendered if isinstance(rendered, list) else [],
            sampling=sampling,
            response_tokens=int(response_tokens),
            duration_ms=int(duration_ms),
            call_id=call_id,
            agent_role=agent_role,
            server_url=server_url,
            model_digest=model_digest,
            model_size_bytes=model_size_bytes,
            response_text=response_text,
            started_at=started_at,
        )
        LLM_CALL_LOG.append(record)
        # Phase 2.1: eager dual-write to the llm_calls table. Failure is
        # logged + swallowed inside mirror_llm_call (JSON drain-into-bundle
        # remains canonical).
        try:
            from core import db_writethrough
            db_writethrough.mirror_llm_call(record)
        except Exception:
            pass
        try:
            from core.metrics import observe_agent_task, observe_llm_call
            observe_agent_task(agent_role, model, duration_ms / 1000.0)
            observe_llm_call(agent_role, model, success=response_tokens > 0)
        except Exception:
            pass
    except Exception as e:
        logger.warning("on_task_completion hook error: %s", e)
