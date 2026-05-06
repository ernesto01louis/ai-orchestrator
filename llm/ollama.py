"""Ollama HTTP client + model-aware URL routing.

The URL cache resolves which server (main vs judge) currently has each
model loaded. Cache TTL is 5 min; resolution falls back to the planner
URL if the cache is empty / model is unknown.
"""
from __future__ import annotations

import json
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
_url_cache: dict = {}
_url_cache_ts: float = 0.0
_URL_CACHE_TTL = 300  # seconds


def _refresh_url_cache():
    global _url_cache, _url_cache_ts
    cache: dict = {}
    # Check each unique server; main is preferred so check last (so it wins on conflict)
    for base_url in dict.fromkeys([OLLAMA_JUDGE_URL, OLLAMA_MAIN_URL]):
        try:
            r = requests.get(f"{base_url}/api/tags", timeout=5)
            if r.ok:
                for m in r.json().get("models", []):
                    cache[m["name"]] = base_url
        except (requests.exceptions.RequestException, ValueError):
            pass
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
def query_ollama(model, prompt, url, run_id) -> LlmResponse:
    log(run_id, f"LLM request -> {model}")
    try:
        r = requests.post(
            url,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=TIMEOUT_LLM_GENERATE,
        )
        r.raise_for_status()
        envelope = r.json()
        return LlmResponse(text=envelope.get("response", ""), envelope=envelope)
    except requests.exceptions.Timeout:
        log(run_id, f"LLM timeout: {model} ({TIMEOUT_LLM_GENERATE}s)")
        return LlmResponse(text="")
    except requests.exceptions.ConnectionError as e:
        log(run_id, f"LLM connection failed for {model}: {e}")
        return LlmResponse(text="")
    except requests.exceptions.HTTPError as e:
        log(run_id, f"LLM HTTP error for {model}: {e}")
        return LlmResponse(text="")
    except json.JSONDecodeError as e:
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
def query_ollama_structured(model, system_prompt, user_prompt, schema, url, run_id) -> LlmResponse:
    """Structured query with JSON-schema enforcement and temperature 0."""
    log(run_id, f"LLM structured request -> {model}")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

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
        if not content:
            log(run_id, f"structured query returned empty content from {model}")
            return LlmResponse(envelope=envelope)
        return LlmResponse(parsed=safe_parse_json(content, run_id, context=model), envelope=envelope)
    except requests.exceptions.Timeout:
        log(run_id, f"LLM timeout: {model} ({TIMEOUT_LLM_STRUCTURED}s)")
        return LlmResponse()
    except requests.exceptions.ConnectionError as e:
        log(run_id, f"LLM connection failed for {model}: {e}")
        return LlmResponse()
    except requests.exceptions.HTTPError as e:
        log(run_id, f"LLM HTTP error for {model}: {e}")
        return LlmResponse()
    except json.JSONDecodeError as e:
        log(run_id, f"LLM returned invalid response envelope for {model}: {e}")
        return LlmResponse()
