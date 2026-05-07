"""Tests for Phase 2.3.2 manual spans on hot paths.

Spans are zero-cost when OTel isn't initialised (the SDK falls back to
the no-op TracerProvider), so these tests don't need a live Tempo.
We register a real ``TracerProvider`` with an ``InMemorySpanExporter``
and assert on the captured spans.
"""
from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _fake_post_factory(json_payload, status_code: int = 200):
    """Build a ``requests.post`` stand-in that returns a MagicMock
    response matching the small subset of the API ollama.py uses."""

    def _post(*_args, **_kwargs):
        resp = MagicMock()
        resp.json.return_value = json_payload
        resp.status_code = status_code
        resp.raise_for_status = MagicMock()
        return resp

    return _post


@pytest.fixture
def in_memory_spans() -> Iterator[InMemorySpanExporter]:
    """Install a fresh TracerProvider that captures spans in memory.

    OTel's global ``trace.set_tracer_provider`` is a one-shot in
    production code — calling it again logs a warning and is ignored.
    For tests we monkeypatch the SDK's ``_TRACER_PROVIDER`` slot
    directly so each test gets a clean exporter.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    original = trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = original  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# llm/ollama spans
# ---------------------------------------------------------------------------


def _find_span(exporter: InMemorySpanExporter, name: str):
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert matches, f"no span named {name!r} (have: {[s.name for s in exporter.get_finished_spans()]})"
    return matches[-1]


def test_query_ollama_emits_generate_span(
    in_memory_spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import ollama as ollama_mod  # noqa: PLC0415

    monkeypatch.setattr(
        ollama_mod, "requests",
        MagicMock(post=_fake_post_factory({"response": "hello", "eval_count": 17})),
    )
    # _get_model_metadata is cached + called from _annotate_envelope; bypass it
    monkeypatch.setattr(ollama_mod, "_get_model_metadata", lambda *_a, **_k: ("abc", 100))

    result = ollama_mod.query_ollama(
        "qwen2.5:72b",
        "hi",
        "http://h:11434/api/generate",
        "test-run-id",
        agent_role="planner",
    )
    assert result.text == "hello"

    span = _find_span(in_memory_spans, "llm.generate")
    attrs = dict(span.attributes or {})
    assert attrs["llm.model"] == "qwen2.5:72b"
    assert attrs["llm.endpoint_kind"] == "generate"
    assert attrs["llm.role"] == "planner"
    assert attrs["orchestrator.run_id"] == "test-run-id"
    assert attrs["llm.eval_count"] == 17
    assert attrs["llm.response_chars"] == 5


def test_query_ollama_records_timeout_outcome(
    in_memory_spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests as requests_mod  # noqa: PLC0415

    from llm import ollama as ollama_mod  # noqa: PLC0415

    fake_requests = MagicMock()
    fake_requests.post.side_effect = requests_mod.exceptions.Timeout("nope")
    fake_requests.exceptions = requests_mod.exceptions
    monkeypatch.setattr(ollama_mod, "requests", fake_requests)

    result = ollama_mod.query_ollama(
        "qwen2.5:72b",
        "hi",
        "http://h:11434/api/generate",
        "test-run-id",
    )
    assert result.text == ""

    span = _find_span(in_memory_spans, "llm.generate")
    attrs = dict(span.attributes or {})
    assert attrs["llm.outcome"] == "timeout"
    # The exception was recorded as a span event.
    assert any(e.name == "exception" for e in (span.events or []))


def test_query_ollama_structured_emits_chat_span(
    in_memory_spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import ollama as ollama_mod  # noqa: PLC0415

    monkeypatch.setattr(
        ollama_mod, "requests",
        MagicMock(post=_fake_post_factory({
            "message": {"content": '{"k": "v"}'},
            "eval_count": 33,
        })),
    )
    monkeypatch.setattr(ollama_mod, "_get_model_metadata", lambda *_a, **_k: ("abc", 100))

    schema = {"type": "object"}
    # Bypass Prefect task wrapping by calling the underlying fn.
    fn = getattr(ollama_mod.query_ollama_structured, "fn",
                 ollama_mod.query_ollama_structured)
    fn(
        "qwen2.5:72b",
        "you are a planner",
        "give me JSON",
        schema,
        "http://h:11434/api/chat",
        "test-run-id",
        agent_role="planner",
    )

    span = _find_span(in_memory_spans, "llm.chat")
    attrs = dict(span.attributes or {})
    assert attrs["llm.endpoint_kind"] == "chat"
    assert attrs["llm.has_schema"] is True
    assert attrs["llm.eval_count"] == 33


# ---------------------------------------------------------------------------
# execution.ssh_command span
# ---------------------------------------------------------------------------


def test_ssh_command_emits_span(
    in_memory_spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    import execution as execution_mod  # noqa: PLC0415

    monkeypatch.setattr(
        execution_mod,
        "SSH_TARGETS",
        {"pi-1": {"host": "192.168.2.95", "username": "louis", "key_path": "/k"}},
    )

    fake_result = MagicMock()
    fake_result.stdout = "out"
    fake_result.stderr = "err"
    fake_result.returncode = 0
    monkeypatch.setattr(
        "execution.subprocess.run",
        lambda *_a, **_k: fake_result,
    )

    out = execution_mod.ssh_command("pi-1", "uptime")
    assert out["returncode"] == 0
    assert out["stdout"] == "out"

    span = _find_span(in_memory_spans, "ssh.command")
    attrs = dict(span.attributes or {})
    assert attrs["ssh.target"] == "pi-1"
    assert attrs["ssh.host"] == "192.168.2.95"
    assert attrs["ssh.username"] == "louis"
    assert attrs["ssh.command_preview"] == "uptime"
    assert attrs["ssh.returncode"] == 0
    assert attrs["ssh.stdout_bytes"] == 3
    assert attrs["ssh.stderr_bytes"] == 3


def test_ssh_command_records_timeout_outcome(
    in_memory_spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess  # noqa: PLC0415

    import execution as execution_mod  # noqa: PLC0415

    monkeypatch.setattr(
        execution_mod,
        "SSH_TARGETS",
        {"pi-1": {"host": "192.168.2.95", "username": "louis", "key_path": "/k"}},
    )

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)

    monkeypatch.setattr("execution.subprocess.run", boom)
    out = execution_mod.ssh_command("pi-1", "sleep 10")
    assert out["returncode"] == -1

    span = _find_span(in_memory_spans, "ssh.command")
    attrs = dict(span.attributes or {})
    assert attrs["ssh.outcome"] == "timeout"
    assert any(e.name == "exception" for e in (span.events or []))


# ---------------------------------------------------------------------------
# core.runtime.log() span event
# ---------------------------------------------------------------------------


def test_log_attaches_event_to_active_span(
    in_memory_spans: InMemorySpanExporter,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When called inside a span, log() must attach an
    ``orchestrator.log`` event with run_id + message attributes."""
    from core import runtime  # noqa: PLC0415
    from core.paths import LOG_DIR  # noqa: PLC0415

    monkeypatch.setattr("core.runtime.LOG_DIR", str(tmp_path))

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent-span"):
        runtime.log("test-run", "hello world")

    span = _find_span(in_memory_spans, "parent-span")
    matching = [
        e for e in (span.events or []) if e.name == "orchestrator.log"
    ]
    assert matching, f"no orchestrator.log event (have: {[e.name for e in (span.events or [])]})"
    attrs = dict(matching[0].attributes or {})
    assert attrs["run_id"] == "test-run"
    assert attrs["message"] == "hello world"
    # LOG_DIR shadowed for cleanup hygiene
    _ = LOG_DIR


def test_log_no_op_outside_span(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside a span, log() must still complete without raising."""
    from core import runtime  # noqa: PLC0415

    monkeypatch.setattr("core.runtime.LOG_DIR", str(tmp_path))
    runtime.log("test-run", "no parent span")
