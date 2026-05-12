"""Tests for references_pkg/web.py — repo-screening firecrawl spike.

Covers the dormant path, the three-condition is_enabled() gate, the
HTTP error path, the empty-response path, the file-write path, the
skip-if-exists idempotence, and the Prom counter labels. The live
firecrawl path is NOT exercised here — it's covered by the live
measurement step in the PR description.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core import config
from references_pkg import web


@pytest.fixture(autouse=True)
def _reset_config(tmp_path: Path) -> Any:
    saved = (
        config.WEB_INGEST_ENABLED,
        config.WEB_INGEST_BASE_URL,
        config.WEB_INGEST_TIMEOUT_SECONDS,
        config.WEB_INGEST_SKIP_IF_EXISTS,
    )
    # Redirect REFERENCE_DIR to a per-test tmpdir so writes don't
    # pollute the live references/ folder.
    saved_dir = web.REFERENCE_DIR
    web.REFERENCE_DIR = tmp_path
    yield
    (
        config.WEB_INGEST_ENABLED,
        config.WEB_INGEST_BASE_URL,
        config.WEB_INGEST_TIMEOUT_SECONDS,
        config.WEB_INGEST_SKIP_IF_EXISTS,
    ) = saved
    web.REFERENCE_DIR = saved_dir


def _mock_response(json_body: Any, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    return resp


# ---------------------------------------------------------------------------
# Dormant + gate behaviour
# ---------------------------------------------------------------------------

def test_dormant_returns_disabled_status() -> None:
    config.WEB_INGEST_ENABLED = False
    result = web.ingest_url("https://example.com")
    assert result.status == "disabled"
    assert result.path is None


def test_invalid_url_returns_invalid_status() -> None:
    config.WEB_INGEST_ENABLED = True
    result = web.ingest_url("not-a-url")
    assert result.status == "invalid_url"


def test_is_enabled_respects_config_flag() -> None:
    config.WEB_INGEST_ENABLED = False
    assert web.is_enabled() is False
    config.WEB_INGEST_ENABLED = True
    assert web.is_enabled() is True


def test_is_enabled_requires_base_url() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = ""
    assert web.is_enabled() is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_ingest_url_writes_markdown_with_frontmatter() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    body = {
        "success": True,
        "data": {
            "markdown": "Hello world\n===========\n\nBody text.",
            "metadata": {
                "title": "Hello Page",
                "statusCode": 200,
                "sourceURL": "https://example.com/hello",
            },
        },
    }

    with patch("references_pkg.web.requests.post", return_value=_mock_response(body)):
        result = web.ingest_url("https://example.com/hello")

    assert result.status == "success"
    assert result.title == "Hello Page"
    assert result.status_code == 200
    assert result.path is not None
    text = result.path.read_text()
    # Frontmatter present + body preserved
    assert text.startswith("---\n")
    assert "source_url: https://example.com/hello" in text
    assert "title: Hello Page" in text
    assert "Hello world" in text


def test_ingest_url_filename_is_url_hash() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    body = {
        "success": True,
        "data": {"markdown": "content", "metadata": {"title": "t", "statusCode": 200}},
    }
    url = "https://example.com/some/path?q=1"
    expected_stem = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

    with patch("references_pkg.web.requests.post", return_value=_mock_response(body)):
        result = web.ingest_url(url)

    assert result.path is not None
    assert result.path.name == f"{expected_stem}.md"


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_skip_if_exists_short_circuits_without_http(tmp_path: Path) -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"
    config.WEB_INGEST_SKIP_IF_EXISTS = True

    url = "https://example.com/cached"
    target = web._target_path(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("pre-existing content")

    with patch("references_pkg.web.requests.post") as mock_post:
        result = web.ingest_url(url)

    assert result.status == "skipped_exists"
    assert result.path == target
    assert result.bytes_written == len("pre-existing content")
    mock_post.assert_not_called()


def test_skip_if_exists_false_overwrites() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"
    config.WEB_INGEST_SKIP_IF_EXISTS = False

    url = "https://example.com/overwrite"
    target = web._target_path(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old content")

    body = {
        "success": True,
        "data": {"markdown": "fresh content", "metadata": {"title": "t", "statusCode": 200}},
    }
    with patch("references_pkg.web.requests.post", return_value=_mock_response(body)):
        result = web.ingest_url(url)

    assert result.status == "success"
    assert "fresh content" in target.read_text()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_http_5xx_returns_http_error() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    with patch("references_pkg.web.requests.post", return_value=_mock_response({}, status=503)):
        result = web.ingest_url("https://example.com")

    assert result.status == "http_error"
    assert result.status_code == 503


def test_connection_error_returns_http_error() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    from requests.exceptions import ConnectionError as ReqConnErr

    with patch("references_pkg.web.requests.post", side_effect=ReqConnErr("boom")):
        result = web.ingest_url("https://example.com")

    assert result.status == "http_error"


def test_success_false_returns_http_error() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    body = {"success": False, "error": "rate limited"}
    with patch("references_pkg.web.requests.post", return_value=_mock_response(body)):
        result = web.ingest_url("https://example.com")

    assert result.status == "http_error"


def test_empty_markdown_returns_empty_status() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    body = {
        "success": True,
        "data": {"markdown": "   \n\n", "metadata": {"title": "Empty", "statusCode": 200}},
    }
    with patch("references_pkg.web.requests.post", return_value=_mock_response(body)):
        result = web.ingest_url("https://example.com/empty")

    assert result.status == "empty"
    assert result.path is None
    assert result.title == "Empty"


def test_malformed_json_returns_http_error() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = json.JSONDecodeError("bad", "<doc>", 0)

    with patch("references_pkg.web.requests.post", return_value=resp):
        result = web.ingest_url("https://example.com")

    assert result.status == "http_error"


# ---------------------------------------------------------------------------
# Prom counter
# ---------------------------------------------------------------------------

def test_success_outcome_bumps_counter() -> None:
    from core import metrics

    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    counter = metrics.WEB_INGEST_TOTAL.labels(outcome="success")
    before = counter._value.get()

    body = {
        "success": True,
        "data": {"markdown": "content", "metadata": {"title": "t", "statusCode": 200}},
    }
    with patch("references_pkg.web.requests.post", return_value=_mock_response(body)):
        web.ingest_url("https://example.com/counter")

    assert counter._value.get() - before == 1


def test_disabled_outcome_bumps_counter() -> None:
    from core import metrics

    config.WEB_INGEST_ENABLED = False
    counter = metrics.WEB_INGEST_TOTAL.labels(outcome="disabled")
    before = counter._value.get()
    web.ingest_url("https://example.com")
    assert counter._value.get() - before == 1


def test_invalid_url_bumps_counter() -> None:
    from core import metrics

    config.WEB_INGEST_ENABLED = True
    counter = metrics.WEB_INGEST_TOTAL.labels(outcome="invalid_url")
    before = counter._value.get()
    web.ingest_url("ftp://nope")
    assert counter._value.get() - before == 1


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------

def test_healthcheck_disabled_returns_false() -> None:
    config.WEB_INGEST_ENABLED = False
    assert web.healthcheck() is False


def test_healthcheck_reachable_returns_true() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    resp = MagicMock()
    resp.status_code = 200
    with patch("references_pkg.web.requests.get", return_value=resp):
        assert web.healthcheck() is True


def test_healthcheck_swallows_connection_error() -> None:
    config.WEB_INGEST_ENABLED = True
    config.WEB_INGEST_BASE_URL = "http://fake-firecrawl:3002"

    from requests.exceptions import ConnectionError as ReqConnErr

    with patch("references_pkg.web.requests.get", side_effect=ReqConnErr("boom")):
        assert web.healthcheck() is False
