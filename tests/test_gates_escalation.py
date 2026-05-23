"""Two-stage Gates auto-promotion.

A repeated failure pattern is promoted to a *warn* gate at
``AUTO_PROMOTE_THRESHOLD`` (=3) occurrences, then auto-escalated to a hard
*block* once it recurs past ``AUTO_BLOCK_THRESHOLD`` (=6). Manual gates and
existing block gates are never touched by consolidation.

All state (gates.json, lessons dir, gates log) is redirected to a tmp dir so
the suite never reads or writes a real store.
"""
from __future__ import annotations

import pytest

import gates

CMD = "deploy flaky-service --force"  # no path/ip/port -> stable extracted pattern


@pytest.fixture
def gate_store(tmp_path, monkeypatch):
    """Point gates.py's module-level paths at a throwaway tmp dir."""
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    monkeypatch.setattr(gates, "GATES_FILE", str(tmp_path / "gates.json"))
    monkeypatch.setattr(gates, "LESSONS_DIR", str(lessons))
    monkeypatch.setattr(gates, "GATES_LOG", str(tmp_path / "gates_log.json"))
    return tmp_path


def _record(cmd: str, n: int) -> None:
    for _ in range(n):
        gates.record_runtime_failure(cmd, "boom: nonzero exit", tool_name="bash")


def _only_gate() -> dict:
    found = gates.load_gates()["gates"]
    assert len(found) == 1, f"expected exactly one gate, got {found}"
    return found[0]


def test_promotes_to_warn_at_threshold(gate_store):
    _record(CMD, 3)
    report = gates.consolidate_lessons.fn()
    assert report["promoted_count"] == 1
    assert report["promoted"][0]["severity"] == "warn"
    assert _only_gate()["severity"] == "warn"


def test_stays_warn_below_block_threshold(gate_store):
    _record(CMD, 5)  # >= AUTO_PROMOTE_THRESHOLD, < AUTO_BLOCK_THRESHOLD
    gates.consolidate_lessons.fn()
    assert _only_gate()["severity"] == "warn"


def test_born_block_when_already_past_block_threshold(gate_store):
    _record(CMD, gates.AUTO_BLOCK_THRESHOLD)
    report = gates.consolidate_lessons.fn()
    assert report["promoted"][0]["severity"] == "block"
    assert _only_gate()["severity"] == "block"


def test_escalates_warn_to_block_on_recurrence(gate_store):
    _record(CMD, 3)
    gates.consolidate_lessons.fn()
    assert _only_gate()["severity"] == "warn"

    _record(CMD, 3)  # total 6 occurrences of the same pattern
    report = gates.consolidate_lessons.fn()
    entry = report["promoted"][0]
    assert entry["severity"] == "block"
    assert entry["escalated"] is True

    gate = _only_gate()  # escalated in place, not duplicated
    assert gate["severity"] == "block"
    assert "escalated" in gate  # stamped by escalate_gate_severity


def test_manual_gate_is_never_auto_escalated(gate_store):
    regex = gates._pattern_to_regex(gates._extract_pattern(CMD))
    gates.add_gate(pattern=regex, reason="operator rule", source="manual", severity="warn")
    _record(CMD, gates.AUTO_BLOCK_THRESHOLD)
    gates.consolidate_lessons.fn()
    gate = _only_gate()
    assert gate["source"] == "manual"
    assert gate["severity"] == "warn"  # left untouched


def test_dry_run_writes_nothing(gate_store):
    _record(CMD, gates.AUTO_BLOCK_THRESHOLD)
    report = gates.consolidate_lessons.fn(dry_run=True)
    assert report["promoted"][0]["would_promote"] is True
    assert gates.load_gates()["gates"] == []


def test_escalate_gate_severity_never_downgrades(gate_store):
    g = gates.add_gate(pattern="rm -rf /x", reason="r", source="manual", severity="block")
    result = gates.escalate_gate_severity(g["id"], "warn")
    assert result["severity"] == "block"  # downgrade refused
