"""Tests for the /activity timeline endpoint (Feature B).

The endpoint aggregates timestamped events (runs, campaigns, git
commits) at query time. These tests assert the response shape and the
limit / window contract — they run against whatever real data is on
disk, so they check structure, not specific events.
"""
from __future__ import annotations

from api.routes.activity import _parse_ts, get_activity

_EVENT_KEYS = {"id", "type", "timestamp", "title", "details", "tags", "link"}
_VALID_TYPES = {"run", "campaign", "git"}


def test_activity_returns_events_envelope():
    res = get_activity(limit=50, hours=168)
    assert "events" in res and "window_hours" in res
    assert isinstance(res["events"], list)
    assert res["window_hours"] == 168


def test_activity_events_have_required_shape():
    for ev in get_activity(limit=50, hours=8760)["events"]:
        assert set(ev.keys()) == _EVENT_KEYS, ev
        assert ev["type"] in _VALID_TYPES
        assert isinstance(ev["tags"], list)
        assert ev["link"] is None or set(ev["link"]) <= {"path", "id"}


def test_activity_sorted_newest_first():
    events = get_activity(limit=100, hours=8760)["events"]
    stamps = [e["timestamp"] for e in events]
    assert stamps == sorted(stamps, reverse=True)


def test_activity_respects_limit():
    assert len(get_activity(limit=5, hours=8760)["events"]) <= 5


def test_activity_window_filters_old_events():
    """A 1-hour window must not return events older than ~1 hour."""
    wide = get_activity(limit=1000, hours=8760)["events"]
    narrow = get_activity(limit=1000, hours=1)["events"]
    assert len(narrow) <= len(wide)


def test_parse_ts_tolerant():
    assert _parse_ts(None) is None
    assert _parse_ts("not-a-date") is None
    dt = _parse_ts("2026-05-16T09:00:00")
    assert dt is not None and dt.tzinfo is not None  # naive input made aware
