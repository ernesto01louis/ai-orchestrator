"""Tests for single-flight lock on llm.ollama._refresh_url_cache.

Verifies that concurrent callers that all pass the TTL check queue on
the lock and only ONE issues the /api/tags HTTP round-trips — the rest
return immediately after seeing a fresh _url_cache_ts inside the lock.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

import llm.ollama as ollama_mod
from llm.ollama import _URL_CACHE_TTL, resolve_chat_url, resolve_generate_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_response(model_names: list[str], *, base_url: str = "") -> MagicMock:
    """Return a fake requests.Response-like object with .ok=True."""
    resp = MagicMock()
    resp.ok = True
    resp.json.return_value = {"models": [{"name": n} for n in model_names]}
    return resp


# ---------------------------------------------------------------------------
# Fixture: reset module-level cache state before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_url_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore _url_cache and _url_cache_ts to pristine state before each test."""
    monkeypatch.setattr("llm.ollama._url_cache", {})
    monkeypatch.setattr("llm.ollama._url_cache_ts", 0.0)


# ---------------------------------------------------------------------------
# Test 1: single-flight under concurrent load
# ---------------------------------------------------------------------------

def test_single_flight_concurrent(monkeypatch: pytest.MonkeyPatch) -> None:
    """N threads racing to refresh the cache issue at most 2 HTTP calls total.

    With a threading.Lock + double-check inside _refresh_url_cache, only the
    first thread to acquire the lock does the actual HTTP work; all others
    see fresh _url_cache_ts and return immediately.
    """
    N = 20
    fake_resp = _make_fake_response(["llama3"], base_url="")
    mock_get = MagicMock(return_value=fake_resp)
    monkeypatch.setattr("llm.ollama.requests.get", mock_get)

    # Force TTL to be expired so every thread thinks a refresh is needed.
    monkeypatch.setattr("llm.ollama._url_cache_ts", time.time() - _URL_CACHE_TTL - 10)

    barrier = threading.Barrier(N)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait()  # all N threads start simultaneously
            resolve_chat_url("llama3")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Worker threads raised: {errors}"
    # 2 unique servers → at most 2 HTTP calls regardless of thread count
    assert mock_get.call_count <= 2, (
        f"Expected ≤2 HTTP calls (single-flight), got {mock_get.call_count}"
    )


# ---------------------------------------------------------------------------
# Test 2: TTL still respected — no extra HTTP calls within TTL
# ---------------------------------------------------------------------------

def test_no_refresh_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful refresh, calls within TTL must not trigger another."""
    fake_resp = _make_fake_response(["mistral"])
    mock_get = MagicMock(return_value=fake_resp)
    monkeypatch.setattr("llm.ollama.requests.get", mock_get)

    # Force expiry so first call refreshes.
    monkeypatch.setattr("llm.ollama._url_cache_ts", time.time() - _URL_CACHE_TTL - 1)
    resolve_chat_url("mistral")
    calls_after_first = mock_get.call_count  # should be 2 (one per server)

    # Now cache is fresh — repeated calls must not grow call_count.
    for _ in range(5):
        resolve_chat_url("mistral")
        resolve_generate_url("mistral")

    assert mock_get.call_count == calls_after_first, (
        f"Unexpected extra HTTP calls within TTL: "
        f"{mock_get.call_count} vs {calls_after_first}"
    )


# ---------------------------------------------------------------------------
# Test 3: TTL expiry triggers exactly one new refresh (2 HTTP calls)
# ---------------------------------------------------------------------------

def test_ttl_expiry_triggers_single_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """After TTL expires a single call to resolve_chat_url issues exactly 2 GETs."""
    fake_resp = _make_fake_response(["gemma"])
    mock_get = MagicMock(return_value=fake_resp)
    monkeypatch.setattr("llm.ollama.requests.get", mock_get)

    # Phase 2.2.4 added a Redis cache fast-path inside _refresh_url_cache —
    # when Redis has a fresh hash, it short-circuits before the HTTP calls.
    # Force the Redis path to miss so we exercise the HTTP fallback.
    monkeypatch.setattr("core.redis_cache.url_cache_get_all", lambda: None)

    # Prime the cache as if it was refreshed recently.
    monkeypatch.setattr("llm.ollama._url_cache_ts", time.time())
    monkeypatch.setattr("llm.ollama._url_cache", {"gemma": "http://fake"})
    assert mock_get.call_count == 0

    # Jump past TTL.
    monkeypatch.setattr("llm.ollama._url_cache_ts", time.time() - _URL_CACHE_TTL - 1)
    resolve_chat_url("gemma")

    # One refresh = 2 HTTP GETs (OLLAMA_JUDGE_URL + OLLAMA_MAIN_URL).
    assert mock_get.call_count == 2, (
        f"Expected exactly 2 HTTP calls after TTL expiry, got {mock_get.call_count}"
    )


# ---------------------------------------------------------------------------
# Test 4: lock doesn't deadlock when requests.get raises
# ---------------------------------------------------------------------------

def test_no_deadlock_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """If all /api/tags requests raise, resolve_chat_url still returns a string."""
    monkeypatch.setattr(
        "llm.ollama.requests.get",
        MagicMock(side_effect=requests.exceptions.ConnectionError("down")),
    )
    monkeypatch.setattr("llm.ollama._url_cache_ts", time.time() - _URL_CACHE_TTL - 1)

    # Must not deadlock or raise.
    result = resolve_chat_url("any-model")
    assert isinstance(result, str)
    assert result.endswith("/api/chat")


# ---------------------------------------------------------------------------
# Test 5: fallback URL used when model not in cache
# ---------------------------------------------------------------------------

def test_fallback_url_when_model_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_chat_url falls back to OLLAMA_PLANNER_URL for unknown models."""
    fake_resp = _make_fake_response(["known-model"])
    monkeypatch.setattr("llm.ollama.requests.get", MagicMock(return_value=fake_resp))
    monkeypatch.setattr("llm.ollama._url_cache_ts", time.time() - _URL_CACHE_TTL - 1)

    url = resolve_chat_url("unknown-model")
    assert url == ollama_mod.OLLAMA_PLANNER_URL + "/api/chat"
