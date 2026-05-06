"""Phase J Scope β: _record_to_llm_call should drop placeholders and map
runtime LlmCallRecord fields directly into the bundle's LlmCall.

Scope α had `call_id=uuid4`, `role="generator"`, `host="ollama-runtime-unknown"`,
`model_digest="sha256-placeholder-scope-beta"`, `model_size_bytes=0`,
`response_text=""`, `started_at=now()-duration_ms`. Scope β captures all
of these in `LlmCallRecord` upstream so the builder no longer needs to
fabricate values.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.llm_call_log import LlmCallRecord
from evidence.builder import _BundleBuilder


def _populated_record(**overrides: object) -> LlmCallRecord:
    started = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    base = dict(
        run_id="r1",
        model="qwen2.5-coder:32b",
        rendered_messages=[{"role": "user", "content": "hi"}],
        sampling={"temperature": 0.5, "seed": 42},
        response_tokens=99,
        duration_ms=1234,
        call_id="task-uuid-stable",
        agent_role="planner",
        server_url="http://192.168.2.13:11434/api/chat",
        model_digest="sha256-deadbeef",
        model_size_bytes=19_000_000_000,
        response_text="here is the plan",
        started_at=started,
    )
    base.update(overrides)
    return LlmCallRecord(**base)  # type: ignore[arg-type]


def test_populated_record_maps_directly_no_placeholders():
    rec = _populated_record()
    call = _BundleBuilder._record_to_llm_call(rec)

    assert call.call_id == "task-uuid-stable"
    assert call.role == "planner"
    assert call.target.role == "planner"
    assert call.target.host == "192.168.2.13:11434"
    assert call.target.model_name == "qwen2.5-coder:32b"
    assert call.target.model_digest == "sha256-deadbeef"
    assert call.target.model_size_bytes == 19_000_000_000
    assert call.response_text == "here is the plan"
    assert call.response_tokens == 99
    assert call.latency_ms == 1234
    assert call.started_at == datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    # Sampling round-trip: temperature pulled out, seed flows via extra="allow"
    assert call.sampling.temperature == 0.5
    assert getattr(call.sampling, "seed") == 42


def test_legacy_record_falls_back_safely():
    """A pre-Scope-β record (no agent_role / digest / started_at) still
    yields a valid LlmCall — fallback role 'generator', fallback host,
    fallback started_at derived from duration_ms."""
    rec = LlmCallRecord(
        run_id="r1", model="m",
        rendered_messages=[], sampling={"temperature": 0.0},
        response_tokens=0, duration_ms=500,
    )
    call = _BundleBuilder._record_to_llm_call(rec)
    assert call.target.role == "generator"
    assert call.role == "generator"
    # No server_url means host falls back to the documented sentinel.
    assert call.target.host  # must be a non-empty string
    assert call.started_at is not None  # always populated


def test_unknown_agent_role_falls_back_to_generator():
    """An agent_role not in the LlmTarget Literal must map to 'generator'."""
    rec = _populated_record(agent_role="bogus-role")
    call = _BundleBuilder._record_to_llm_call(rec)
    assert call.target.role == "generator"
    # The free-form LlmCall.role mirrors the validated literal so the bundle
    # stays internally consistent.
    assert call.role == "generator"


def test_host_extracted_from_generate_url():
    rec = _populated_record(server_url="http://10.0.0.5:11434/api/generate")
    call = _BundleBuilder._record_to_llm_call(rec)
    assert call.target.host == "10.0.0.5:11434"


def test_each_role_passes_validation():
    for role in ("planner", "judge", "generator", "optimizer",
                 "troubleshooter", "tool_dispatch"):
        rec = _populated_record(agent_role=role)
        call = _BundleBuilder._record_to_llm_call(rec)
        assert call.target.role == role
        assert call.role == role
