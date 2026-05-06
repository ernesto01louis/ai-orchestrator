"""Tests for Phase J β: Ollama model metadata cache + envelope injection.

`llm.ollama._get_model_metadata` queries `/api/show` once per model per
process and caches the result. `query_ollama` and `query_ollama_structured`
embed the cached digest + size_bytes (and the response text body) into
the LlmResponse envelope under `_orchestrator_*` keys so the state hook
can populate citation-grade LlmCallRecord fields without a second
network round-trip.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from llm import ollama as ollama_mod
from llm.ollama import (
    _get_model_metadata,
    _strip_endpoint,
    query_ollama,
    query_ollama_structured,
)


def _reset_metadata_cache() -> None:
    """Clear the module-level cache between tests."""
    ollama_mod._model_metadata_cache.clear()


def test_strip_endpoint_handles_chat_and_generate():
    assert _strip_endpoint("http://h:11434/api/chat") == "http://h:11434"
    assert _strip_endpoint("http://h:11434/api/generate") == "http://h:11434"
    assert _strip_endpoint("http://h:11434/api/show") == "http://h:11434"
    # Idempotent on bare base
    assert _strip_endpoint("http://h:11434") == "http://h:11434"


def test_get_model_metadata_caches_per_model():
    _reset_metadata_cache()
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json.return_value = {
        "digest": "sha256-abc123",
        "size": 19_000_000_000,
    }
    with patch("llm.ollama.requests.post", return_value=fake) as post:
        a = _get_model_metadata("qwen2.5", "http://h:11434")
        b = _get_model_metadata("qwen2.5", "http://h:11434")
    assert a == ("sha256-abc123", 19_000_000_000)
    assert a is b or a == b
    assert post.call_count == 1, "second call must be cache hit"


def test_get_model_metadata_returns_empty_on_error():
    _reset_metadata_cache()
    with patch("llm.ollama.requests.post", side_effect=ConnectionError("nope")):
        out = _get_model_metadata("missing-model", "http://h:11434")
    assert out == ("", 0)


def test_query_ollama_injects_metadata_into_envelope():
    _reset_metadata_cache()
    fake_show = MagicMock()
    fake_show.raise_for_status = MagicMock()
    fake_show.json.return_value = {"digest": "sha256-d1", "size": 1234}

    fake_gen = MagicMock()
    fake_gen.raise_for_status = MagicMock()
    fake_gen.json.return_value = {"response": "hello world", "eval_count": 3}

    def post_router(url, **_kwargs):
        if url.endswith("/api/show"):
            return fake_show
        return fake_gen

    with patch("llm.ollama.requests.post", side_effect=post_router):
        out = query_ollama.fn("qwen2.5", "ping", "http://h:11434/api/generate", "rB")

    assert out.text == "hello world"
    assert out.envelope.get("_orchestrator_digest") == "sha256-d1"
    assert out.envelope.get("_orchestrator_size_bytes") == 1234
    assert out.envelope.get("_orchestrator_response_text") == "hello world"


def test_query_ollama_structured_injects_response_text_from_message_content():
    _reset_metadata_cache()
    fake_show = MagicMock()
    fake_show.raise_for_status = MagicMock()
    fake_show.json.return_value = {"digest": "sha256-d2", "size": 9999}

    fake_chat = MagicMock()
    fake_chat.raise_for_status = MagicMock()
    fake_chat.json.return_value = {
        "message": {"content": '{"x":1}'},
        "eval_count": 4,
    }

    def post_router(url, **_kwargs):
        if url.endswith("/api/show"):
            return fake_show
        return fake_chat

    with patch("llm.ollama.requests.post", side_effect=post_router):
        out = query_ollama_structured.fn(
            "qwen2.5", "sysprompt", "userprompt",
            {"type": "object"}, "http://h:11434/api/chat", "rC",
        )

    assert out.parsed == {"x": 1}
    assert out.envelope.get("_orchestrator_digest") == "sha256-d2"
    assert out.envelope.get("_orchestrator_size_bytes") == 9999
    assert out.envelope.get("_orchestrator_response_text") == '{"x":1}'


def test_query_ollama_accepts_agent_role_kwarg():
    """agent_role propagates into envelope so state hook can record it."""
    _reset_metadata_cache()
    fake_show = MagicMock()
    fake_show.raise_for_status = MagicMock()
    fake_show.json.return_value = {"digest": "", "size": 0}
    fake_gen = MagicMock()
    fake_gen.raise_for_status = MagicMock()
    fake_gen.json.return_value = {"response": "x", "eval_count": 1}

    def post_router(url, **_kwargs):
        if url.endswith("/api/show"):
            return fake_show
        return fake_gen

    with patch("llm.ollama.requests.post", side_effect=post_router):
        out = query_ollama.fn(
            "qwen2.5", "p", "http://h:11434/api/generate", "rD",
            agent_role="judge",
        )

    assert out.envelope.get("_orchestrator_agent_role") == "judge"
