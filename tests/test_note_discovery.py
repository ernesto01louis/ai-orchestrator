"""Phase 3.3 NoteDiscovery REST client tests.

Mocks ``requests.get`` to exercise every code path without hitting the
real container. Covers the disabled / empty / failure / success
outcomes plus snippet handling and trace persistence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import core.note_discovery as nd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(json_body: Any, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    if status >= 400:
        resp.raise_for_status.side_effect = nd.requests.HTTPError(f"{status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture(autouse=True)
def _reset_module_globals() -> Any:
    """Some tests flip module-level constants for force-enable; restore."""
    saved = (
        nd.NOTEDISCOVERY_ENABLED,
        nd.NOTEDISCOVERY_BASE_URL,
        nd.NOTEDISCOVERY_TOP_K,
        nd.NOTEDISCOVERY_TIMEOUT_SECONDS,
    )
    yield
    (
        nd.NOTEDISCOVERY_ENABLED,
        nd.NOTEDISCOVERY_BASE_URL,
        nd.NOTEDISCOVERY_TOP_K,
        nd.NOTEDISCOVERY_TIMEOUT_SECONDS,
    ) = saved


# ---------------------------------------------------------------------------
# is_enabled gate
# ---------------------------------------------------------------------------

def test_is_enabled_off_by_default() -> None:
    """Dormant by config — even with a base_url, enabled=false short-circuits."""
    nd.NOTEDISCOVERY_ENABLED = False
    nd.NOTEDISCOVERY_BASE_URL = "http://192.168.2.203:8010"
    assert nd.is_enabled() is False


def test_is_enabled_true_when_flag_and_url_set() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    assert nd.is_enabled() is True


def test_is_enabled_false_when_url_missing() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = ""
    assert nd.is_enabled() is False


# ---------------------------------------------------------------------------
# _strip_marks
# ---------------------------------------------------------------------------

def test_strip_marks_removes_html() -> None:
    text = '...some <mark class="search-highlight">match</mark> here...'
    assert nd._strip_marks(text) == "...some match here..."


def test_strip_marks_handles_multiple() -> None:
    text = 'a <mark>x</mark> b <mark class="y">y</mark> c'
    assert nd._strip_marks(text) == "a x b y c"


def test_strip_marks_idempotent_on_plain() -> None:
    assert nd._strip_marks("plain text") == "plain text"


# ---------------------------------------------------------------------------
# search_notes
# ---------------------------------------------------------------------------

def test_search_notes_short_circuit_when_disabled() -> None:
    nd.NOTEDISCOVERY_ENABLED = False
    with patch("core.note_discovery.requests.get") as get:
        out = nd.search_notes("anything")
    assert out == []
    assert get.call_count == 0


def test_search_notes_short_circuit_on_empty_query() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    with patch("core.note_discovery.requests.get") as get:
        out = nd.search_notes("")
    assert out == []
    assert get.call_count == 0


def test_search_notes_returns_empty_on_request_failure() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    with patch(
        "core.note_discovery.requests.get",
        side_effect=nd.requests.ConnectionError("nope"),
    ):
        out = nd.search_notes("x")
    assert out == []


def test_search_notes_parses_results_and_strips_marks() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    body = {
        "results": [
            {
                "name": "note-a",
                "path": "research/a.md",
                "folder": "research",
                "matches": [
                    {"line_number": 1, "context": '...<mark class="x">hit</mark> here...'},
                    {"line_number": 5, "context": "another"},
                ],
            },
            {
                "name": "note-b",
                "path": "daily/b.md",
                "folder": "daily",
                "matches": [],
            },
        ],
        "query": "x",
        "pagination": {"limit": 8, "offset": 0, "total": 2, "has_more": False},
    }
    with patch("core.note_discovery.requests.get", return_value=_make_response(body)):
        notes = nd.search_notes("x")
    assert len(notes) == 2
    assert notes[0].name == "note-a"
    assert "hit" in notes[0].snippet
    assert "<mark" not in notes[0].snippet
    assert "another" in notes[0].snippet
    assert notes[1].snippet == ""


def test_search_notes_returns_empty_on_non_dict_body() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    with patch("core.note_discovery.requests.get", return_value=_make_response([])):
        out = nd.search_notes("x")
    assert out == []


def test_search_notes_respects_top_k() -> None:
    """Server returns 5 results but caller asked for top_k=2."""
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    body = {
        "results": [
            {"name": f"n-{i}", "path": f"p/{i}.md", "folder": "x", "matches": []}
            for i in range(5)
        ],
    }
    with patch("core.note_discovery.requests.get", return_value=_make_response(body)) as get:
        out = nd.search_notes("x", top_k=2)
    assert len(out) == 2
    # Limit is forwarded as a request param.
    _, kwargs = get.call_args
    assert kwargs["params"]["limit"] == "2"


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------

def test_healthcheck_returns_true_on_healthy() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    with patch(
        "core.note_discovery.requests.get",
        return_value=_make_response({"status": "healthy", "app": "NoteDiscovery"}),
    ):
        assert nd.healthcheck() is True


def test_healthcheck_returns_false_on_non_200() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    with patch(
        "core.note_discovery.requests.get",
        return_value=_make_response({}, status=503),
    ):
        assert nd.healthcheck() is False


def test_healthcheck_returns_false_when_disabled() -> None:
    nd.NOTEDISCOVERY_ENABLED = False
    assert nd.healthcheck() is False


def test_healthcheck_returns_false_on_connection_error() -> None:
    nd.NOTEDISCOVERY_ENABLED = True
    nd.NOTEDISCOVERY_BASE_URL = "http://localhost:8010"
    with patch(
        "core.note_discovery.requests.get",
        side_effect=nd.requests.ConnectionError("boom"),
    ):
        assert nd.healthcheck() is False


# ---------------------------------------------------------------------------
# Evidence-bundle wiring (3.3.4)
# ---------------------------------------------------------------------------

def test_planner_research_trace_round_trips_to_evidence_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop a planner_research.json under MEMORY_DIR/<run_id>/ and
    confirm _BundleBuilder._collect_planner_references picks it up.
    """
    from core.evidence import RunRecord
    from evidence.builder import _BundleBuilder

    monkeypatch.setattr("evidence.builder.MEMORY_DIR", str(tmp_path))

    run_id = "test-run-1"
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "planner_research.json").write_text(json.dumps({
        "run_id": run_id,
        "results": [
            {"name": "n-a", "path": "x/a.md", "folder": "x", "snippet": "short"},
            {"name": "n-b", "path": "y/b.md", "folder": "y", "snippet": "longer snippet here"},
        ],
    }))

    runs = [
        RunRecord(
            run_id=run_id, parameters={}, status="success",
            started_at="2026-05-08T00:00:00Z",
            finished_at="2026-05-08T00:01:00Z",
        ),
    ]

    builder = _BundleBuilder.__new__(_BundleBuilder)
    refs = _BundleBuilder._collect_planner_references(builder, runs)

    paths = sorted(r.path for r in refs)
    assert paths == ["x/a.md", "y/b.md"]
    name_for_a = next(r for r in refs if r.path == "x/a.md")
    assert name_for_a.snippet == "short"
