"""Tests for core.llm_call_log — per-run-id buffer for LlmCall records."""
import threading
from datetime import datetime, timezone

from core.llm_call_log import LlmCallLogger, LlmCallRecord


def test_append_and_drain_round_trip():
    logger = LlmCallLogger()
    rec = LlmCallRecord(
        run_id="r1",
        model="qwen2.5-coder:32b",
        rendered_messages=[{"role": "user", "content": "hi"}],
        sampling={"temperature": 0.0},
        response_tokens=42,
        duration_ms=123,
    )
    logger.append(rec)
    drained = logger.drain("r1")
    assert drained == [rec]
    assert logger.drain("r1") == []  # second drain is empty


def test_per_run_id_isolation():
    logger = LlmCallLogger()
    a = LlmCallRecord(run_id="a", model="m", rendered_messages=[],
                     sampling={}, response_tokens=1, duration_ms=1)
    b = LlmCallRecord(run_id="b", model="m", rendered_messages=[],
                     sampling={}, response_tokens=2, duration_ms=2)
    logger.append(a)
    logger.append(b)
    assert logger.drain("a") == [a]
    assert logger.drain("b") == [b]


def test_thread_safe_concurrent_appends():
    logger = LlmCallLogger()

    def worker(n):
        for i in range(n):
            logger.append(LlmCallRecord(
                run_id="shared", model="m", rendered_messages=[],
                sampling={}, response_tokens=i, duration_ms=i,
            ))

    threads = [threading.Thread(target=worker, args=(50,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    drained = logger.drain("shared")
    assert len(drained) == 200


def test_drain_unknown_run_id_returns_empty():
    logger = LlmCallLogger()
    assert logger.drain("nope") == []


def test_record_includes_all_required_fields():
    rec = LlmCallRecord(
        run_id="r1",
        model="m",
        rendered_messages=[{"role": "system", "content": "s"}],
        sampling={"temperature": 0.7, "max_tokens": 1024},
        response_tokens=99,
        duration_ms=2500,
    )
    assert rec.run_id == "r1"
    assert rec.model == "m"
    assert rec.rendered_messages[0]["content"] == "s"
    assert rec.sampling["max_tokens"] == 1024
    assert rec.response_tokens == 99
    assert rec.duration_ms == 2500


def test_record_citation_grade_fields_default_empty():
    """Phase J β: new fields default to safe empties so legacy callers stay valid."""
    rec = LlmCallRecord(
        run_id="r1",
        model="m",
        rendered_messages=[],
        sampling={},
        response_tokens=0,
        duration_ms=0,
    )
    assert rec.call_id == ""
    assert rec.agent_role == ""
    assert rec.server_url == ""
    assert rec.model_digest == ""
    assert rec.model_size_bytes == 0
    assert rec.response_text == ""
    assert rec.started_at is None


def test_record_citation_grade_fields_round_trip():
    """Phase J β: every new field round-trips through append/drain."""
    started = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    rec = LlmCallRecord(
        run_id="rB",
        model="qwen2.5-coder:32b",
        rendered_messages=[{"role": "user", "content": "hi"}],
        sampling={"temperature": 0.0},
        response_tokens=42,
        duration_ms=123,
        call_id="task-run-uuid-abc",
        agent_role="planner",
        server_url="http://192.168.2.13:11434/api/chat",
        model_digest="sha256-deadbeef",
        model_size_bytes=19_000_000_000,
        response_text="here is the plan...",
        started_at=started,
    )

    logger = LlmCallLogger()
    logger.append(rec)
    drained = logger.drain("rB")
    assert len(drained) == 1
    out = drained[0]
    assert out.call_id == "task-run-uuid-abc"
    assert out.agent_role == "planner"
    assert out.server_url == "http://192.168.2.13:11434/api/chat"
    assert out.model_digest == "sha256-deadbeef"
    assert out.model_size_bytes == 19_000_000_000
    assert out.response_text == "here is the plan..."
    assert out.started_at == started
