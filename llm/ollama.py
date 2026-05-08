"""Ollama HTTP client + model-aware URL routing.

The URL cache resolves which server (main vs judge) currently has each
model loaded. Cache TTL is 5 min; resolution falls back to the planner
URL if the cache is empty / model is unknown.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from prefect import task
from prefect.cache_policies import NO_CACHE

from core.config import (
    OLLAMA_JUDGE_URL,
    OLLAMA_MAIN_URL,
    OLLAMA_PLANNER_URL,
    TIMEOUT_LLM_GENERATE,
    TIMEOUT_LLM_STRUCTURED,
)
from core.runtime import log
from prefect_io.state_hooks import on_task_completion

from .repair import safe_parse_json


@dataclass
class LlmResponse:
    """Carries Ollama output + full envelope so state hooks can extract
    eval_count without a side channel. Returned by query_ollama (text path)
    and query_ollama_structured (parsed path); the unused field stays at
    its default."""
    text: str = ""
    parsed: Any = None
    envelope: dict[str, Any] = field(default_factory=dict)

    @property
    def eval_count(self) -> int:
        return int(self.envelope.get("eval_count") or 0)


# ── model-aware URL routing ────────────────────────
_url_cache: dict[str, str] = {}
_url_cache_ts: float = 0.0
_URL_CACHE_TTL = 300  # seconds
_url_cache_lock = threading.Lock()


# ── /api/show metadata cache (Phase J β) ───────────
_model_metadata_cache: dict[str, tuple[str, int]] = {}
_model_metadata_lock = threading.Lock()


def _strip_endpoint(url: str) -> str:
    """`http://host:port/api/chat` → `http://host:port`. Idempotent on bare bases."""
    for suffix in ("/api/chat", "/api/generate", "/api/show", "/api/tags"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _get_model_metadata(model: str, base_url: str) -> tuple[str, int]:
    """Return ``(digest, size_bytes)`` for ``model`` at ``base_url``.

    Cached process-wide on first hit. On any failure (connection, HTTP,
    parse) returns ``("", 0)`` — citation-grade fidelity is best-effort,
    so a missing digest must never fail the LLM call itself.
    """
    with _model_metadata_lock:
        cached = _model_metadata_cache.get(model)
    if cached is not None:
        return cached
    digest, size_bytes = "", 0
    try:
        r = requests.post(f"{base_url}/api/show", json={"name": model}, timeout=5)
        r.raise_for_status()
        data = r.json()
        digest = str(data.get("digest", "") or "")
        size_bytes = int(data.get("size", 0) or 0)
    except (requests.exceptions.RequestException, ConnectionError,
            OSError, ValueError, TypeError):
        digest, size_bytes = "", 0
    with _model_metadata_lock:
        _model_metadata_cache[model] = (digest, size_bytes)
    return digest, size_bytes


def _annotate_envelope(
    envelope: dict[str, Any],
    *,
    model: str,
    url: str,
    agent_role: str,
    response_text: str,
) -> dict[str, Any]:
    """Inject Phase J β citation-grade fields into the LlmResponse envelope.

    State-hook reads these `_orchestrator_*` keys to populate LlmCallRecord
    without a second network round-trip. ``url`` is the full endpoint URL
    (e.g. /api/chat); we strip to base before looking up metadata.
    """
    digest, size_bytes = _get_model_metadata(model, _strip_endpoint(url))
    envelope.setdefault("_orchestrator_digest", digest)
    envelope.setdefault("_orchestrator_size_bytes", size_bytes)
    envelope.setdefault("_orchestrator_response_text", response_text)
    envelope.setdefault("_orchestrator_agent_role", agent_role)
    return envelope


def _refresh_url_cache() -> None:
    """Repopulate the model→server cache.

    Phase 2.2.4: when Redis is enabled, prefer the cross-instance hash
    in Redis as the source of truth. If Redis has a fresh entry, hydrate
    the in-process dict from it and skip the ``/api/tags`` round-trip.
    Otherwise fall back to the today's behaviour (single-flight refresh
    against each Ollama server) and write the result back to Redis so
    the next instance to refresh shares it.
    """
    global _url_cache, _url_cache_ts

    # Cheap fast-path: if Redis already has this round's cache, skip
    # acquiring _url_cache_lock entirely. ``url_cache_get_all`` is a
    # single HGETALL when Redis is enabled, returns ``None`` otherwise.
    from core import redis_cache  # noqa: PLC0415
    cached_from_redis = redis_cache.url_cache_get_all()
    if cached_from_redis:
        with _url_cache_lock:
            _url_cache = cached_from_redis
            _url_cache_ts = time.time()
        return

    with _url_cache_lock:
        # Double-check: another thread may have refreshed while we waited.
        if time.time() - _url_cache_ts <= _URL_CACHE_TTL:
            return
        cache: dict[str, str] = {}
        # Check each unique server; main is preferred so check last (so it wins on conflict)
        for base_url in dict.fromkeys([OLLAMA_JUDGE_URL, OLLAMA_MAIN_URL]):
            try:
                r = requests.get(f"{base_url}/api/tags", timeout=5)
                if r.ok:
                    for m in r.json().get("models", []):
                        cache[m["name"]] = base_url
            except (requests.exceptions.RequestException, ValueError):
                pass
        # Push the freshly-built cache to Redis so other instances
        # share this round (no-op when Redis is disabled).
        from core import config as _config  # noqa: PLC0415
        redis_cache.url_cache_store(cache, _config.REDIS_URL_CACHE_TTL)
        _url_cache = cache
        _url_cache_ts = time.time()


def resolve_chat_url(model: str) -> str:
    """Return the /api/chat URL for the server that has this model."""
    if time.time() - _url_cache_ts > _URL_CACHE_TTL:
        _refresh_url_cache()
    base = _url_cache.get(model, OLLAMA_PLANNER_URL)
    return base + "/api/chat"


def resolve_generate_url(model: str) -> str:
    """Return the /api/generate URL for the server that has this model."""
    if time.time() - _url_cache_ts > _URL_CACHE_TTL:
        _refresh_url_cache()
    base = _url_cache.get(model, OLLAMA_MAIN_URL)
    return base + "/api/generate"


# ── lightweight API probes ─────────────────────────
def query_ollama_api(base_url: str, endpoint: str, timeout: int = 10):
    """Quick GET to an Ollama API endpoint. Returns parsed JSON or None."""
    try:
        r = requests.get(f"{base_url}{endpoint}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError):
        return None


# ── free-form generation (/api/generate) ───────────
@task(
    name="query_ollama",
    tags={"llm-call"},
    retries=3,
    retry_delay_seconds=[1, 5, 15],
    cache_policy=NO_CACHE,
    on_completion=[on_task_completion],
    on_failure=[on_task_completion],
)
def query_ollama(model, prompt, url, run_id, agent_role: str = "") -> LlmResponse:
    from core.otel import get_tracer  # noqa: PLC0415
    tracer = get_tracer("ai-orchestrator.llm")

    # Phase 3.1 HITL — co_pilot mode pauses BEFORE the call so the
    # operator can edit the prompt; step_by_step pauses AFTER. Both
    # are inert when hitl_mode is full_auto / gate_only / checkpoint.
    try:
        from core.hitl import hitl_checkpoint  # noqa: PLC0415
        edit = hitl_checkpoint(run_id, "pre_llm")
        if edit and edit.get("action") == "edit" and edit.get("prompt"):
            prompt = str(edit["prompt"])
    except Exception:  # noqa: BLE001 — never let HITL break an LLM call
        pass

    log(run_id, f"LLM request -> {model}")
    with tracer.start_as_current_span("llm.generate") as span:
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.url", url)
        span.set_attribute("llm.endpoint_kind", "generate")
        span.set_attribute("llm.role", agent_role or "")
        span.set_attribute("orchestrator.run_id", run_id or "")
        try:
            r = requests.post(
                url,
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=TIMEOUT_LLM_GENERATE,
            )
            r.raise_for_status()
            envelope = r.json()
            text = envelope.get("response", "")
            span.set_attribute("llm.eval_count", int(envelope.get("eval_count", 0) or 0))
            span.set_attribute("llm.response_chars", len(text))
            envelope = _annotate_envelope(
                envelope, model=model, url=url,
                agent_role=agent_role, response_text=text,
            )
            # Phase 3.1 HITL — step_by_step pauses AFTER the call.
            try:
                from core.hitl import hitl_checkpoint  # noqa: PLC0415
                hitl_checkpoint(run_id, "post_llm")
            except Exception:  # noqa: BLE001 — never let HITL break an LLM call
                pass
            return LlmResponse(text=text, envelope=envelope)
        except requests.exceptions.Timeout as e:
            span.set_attribute("llm.outcome", "timeout")
            span.record_exception(e)
            log(run_id, f"LLM timeout: {model} ({TIMEOUT_LLM_GENERATE}s)")
            return LlmResponse(text="")
        except requests.exceptions.ConnectionError as e:
            span.set_attribute("llm.outcome", "connection_error")
            span.record_exception(e)
            log(run_id, f"LLM connection failed for {model}: {e}")
            return LlmResponse(text="")
        except requests.exceptions.HTTPError as e:
            span.set_attribute("llm.outcome", "http_error")
            span.record_exception(e)
            log(run_id, f"LLM HTTP error for {model}: {e}")
            return LlmResponse(text="")
        except json.JSONDecodeError as e:
            span.set_attribute("llm.outcome", "invalid_json")
            span.record_exception(e)
            log(run_id, f"LLM returned invalid JSON for {model}: {e}")
            return LlmResponse(text="")


# ── structured chat (/api/chat with format) ────────
@task(
    name="query_ollama_structured",
    tags={"llm-call"},
    retries=3,
    retry_delay_seconds=[1, 5, 15],
    cache_policy=NO_CACHE,
    on_completion=[on_task_completion],
    on_failure=[on_task_completion],
)
def query_ollama_structured(model, system_prompt, user_prompt, schema, url, run_id,
                             agent_role: str = "") -> LlmResponse:
    """Structured query with JSON-schema enforcement and temperature 0."""
    from core.otel import get_tracer  # noqa: PLC0415
    tracer = get_tracer("ai-orchestrator.llm")

    # Phase 3.1 HITL — co_pilot lets the operator edit user_prompt
    # before the call. step_by_step pauses post-call (below).
    try:
        from core.hitl import hitl_checkpoint  # noqa: PLC0415
        edit = hitl_checkpoint(run_id, "pre_llm")
        if edit and edit.get("action") == "edit" and edit.get("prompt"):
            user_prompt = str(edit["prompt"])
    except Exception:  # noqa: BLE001
        pass

    log(run_id, f"LLM structured request -> {model}")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    with tracer.start_as_current_span("llm.chat") as span:
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.url", url)
        span.set_attribute("llm.endpoint_kind", "chat")
        span.set_attribute("llm.role", agent_role or "")
        span.set_attribute("orchestrator.run_id", run_id or "")
        span.set_attribute("llm.has_schema", schema is not None)
        try:
            r = requests.post(
                url,
                json={
                    "model": model,
                    "messages": messages,
                    "format": schema,
                    "stream": False,
                    "options": {"temperature": 0},
                },
                timeout=TIMEOUT_LLM_STRUCTURED,
            )
            r.raise_for_status()
            envelope = r.json()
            content = envelope.get("message", {}).get("content", "")
            span.set_attribute("llm.eval_count", int(envelope.get("eval_count", 0) or 0))
            span.set_attribute("llm.response_chars", len(content))
            envelope = _annotate_envelope(
                envelope, model=model, url=url,
                agent_role=agent_role, response_text=content,
            )
            if not content:
                span.set_attribute("llm.outcome", "empty_content")
                log(run_id, f"structured query returned empty content from {model}")
                return LlmResponse(envelope=envelope)
            # Phase 3.1 HITL — step_by_step pauses AFTER the call.
            try:
                from core.hitl import hitl_checkpoint  # noqa: PLC0415
                hitl_checkpoint(run_id, "post_llm")
            except Exception:  # noqa: BLE001
                pass
            return LlmResponse(parsed=safe_parse_json(content, run_id, context=model), envelope=envelope)
        except requests.exceptions.Timeout as e:
            span.set_attribute("llm.outcome", "timeout")
            span.record_exception(e)
            log(run_id, f"LLM timeout: {model} ({TIMEOUT_LLM_STRUCTURED}s)")
            return LlmResponse()
        except requests.exceptions.ConnectionError as e:
            span.set_attribute("llm.outcome", "connection_error")
            span.record_exception(e)
            log(run_id, f"LLM connection failed for {model}: {e}")
            return LlmResponse()
        except requests.exceptions.HTTPError as e:
            span.set_attribute("llm.outcome", "http_error")
            span.record_exception(e)
            log(run_id, f"LLM HTTP error for {model}: {e}")
            return LlmResponse()
        except json.JSONDecodeError as e:
            span.set_attribute("llm.outcome", "invalid_json")
            span.record_exception(e)
            log(run_id, f"LLM returned invalid response envelope for {model}: {e}")
            return LlmResponse()
