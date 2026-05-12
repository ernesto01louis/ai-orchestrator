"""Repo-screening spike (2026-05-12): firecrawl-backed web ingestion.

Single public entry point — ``ingest_url(url) -> Path | None`` — posts to
the self-hosted firecrawl ``/v2/scrape`` endpoint and writes the
returned markdown into ``references/web/<sha256>.md`` so it flows
through the existing ``load_reference_content`` pipeline like a PDF.

The orchestrator never crawls autonomously. Operators call
``ingest_url`` from a script, MCP tool, or one-off REPL — same shape
as the ``measure_*`` harness scripts shipped with the prior two
repo-screening spikes.

Three-condition ``is_enabled()`` gate (config flag + base_url set +
``requests`` importable) matches the Phase 3.3 NoteDiscovery shape.

Failure handling: any HTTP error / timeout / malformed response
returns ``None`` and bumps the Prom counter with the failure outcome.
The wrapper never raises out — web ingestion is best-effort.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from core import config as _config
from core.metrics import observe_web_ingest
from core.paths import REFERENCE_DIR

logger = logging.getLogger(__name__)

# Subdirectory under REFERENCE_DIR for firecrawl-sourced markdown.
# Keeps web ingests visually distinct from operator-curated PDFs.
WEB_REFERENCE_SUBDIR = "web"

# Same cap as MAX_REFERENCE_UPLOAD_BYTES — refuse a page larger than this.
MAX_WEB_PAGE_BYTES = 50 * 1024 * 1024  # 50 MB

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


@dataclass(frozen=True)
class IngestResult:
    """One scrape outcome.

    ``path`` is the saved markdown file or ``None`` on error/skip.
    ``status`` is one of {success, skipped_exists, http_error, empty,
    disabled, invalid_url}. ``title`` and ``status_code`` are pulled
    from the firecrawl response metadata when available.
    """

    url: str
    status: str
    path: Path | None = None
    title: str = ""
    status_code: int = 0
    bytes_written: int = 0


def is_enabled() -> bool:
    """Three-condition gate: config flag + base_url + ``requests``."""
    if not _config.WEB_INGEST_ENABLED:
        return False
    if not _config.WEB_INGEST_BASE_URL:
        return False
    # ``requests`` is in base requirements; this branch is symmetric
    # with the NoteDiscovery client and survives a hypothetical swap.
    try:
        import requests as _r  # noqa: F401
    except ImportError:
        return False
    return True


def healthcheck() -> bool:
    """Cheap probe of the firecrawl LXC. Returns True iff
    ``GET <base_url>/`` responds 200 within 5s. Never raises."""
    if not is_enabled():
        return False
    try:
        resp = requests.get(_config.WEB_INGEST_BASE_URL.rstrip("/") + "/", timeout=5)
        return bool(resp.status_code == 200)
    except (requests.RequestException, OSError):
        return False


def _sanitized_filename(url: str) -> str:
    """SHA-256 of the URL is the on-disk filename. Avoids URL-encoding
    edge cases (trailing slashes, query strings, percent-encoded chars)
    and keeps filenames predictable + filesystem-safe."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _target_path(url: str) -> Path:
    """Resolve the on-disk target for a URL."""
    base = REFERENCE_DIR / WEB_REFERENCE_SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{_sanitized_filename(url)}.md"


def ingest_url(url: str) -> IngestResult:
    """Scrape ``url`` via firecrawl and persist the markdown.

    Returns an ``IngestResult`` with ``status`` describing the outcome.
    Never raises — every failure path returns an ``IngestResult`` with
    a non-success status and bumps the Prom counter.
    """
    started = time.monotonic()

    if not _URL_RE.match(url or ""):
        observe_web_ingest(outcome="invalid_url", duration_seconds=0.0)
        return IngestResult(url=url, status="invalid_url")

    if not is_enabled():
        observe_web_ingest(outcome="disabled", duration_seconds=0.0)
        return IngestResult(url=url, status="disabled")

    target = _target_path(url)
    if _config.WEB_INGEST_SKIP_IF_EXISTS and target.exists():
        # Operators get an idempotent re-call — ingest twice, same path,
        # no overwrite of a manually-curated note.
        observe_web_ingest(
            outcome="skipped_exists",
            duration_seconds=time.monotonic() - started,
        )
        return IngestResult(
            url=url,
            status="skipped_exists",
            path=target,
            bytes_written=target.stat().st_size,
        )

    base = _config.WEB_INGEST_BASE_URL.rstrip("/")
    timeout = _config.WEB_INGEST_TIMEOUT_SECONDS

    try:
        resp = requests.post(
            f"{base}/v2/scrape",
            json={"url": url},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("[web_ingest] request failed: %s", exc)
        observe_web_ingest(
            outcome="http_error",
            duration_seconds=time.monotonic() - started,
        )
        return IngestResult(url=url, status="http_error")

    if resp.status_code != 200:
        logger.warning(
            "[web_ingest] firecrawl returned HTTP %d for %s", resp.status_code, url,
        )
        observe_web_ingest(
            outcome="http_error",
            duration_seconds=time.monotonic() - started,
        )
        return IngestResult(url=url, status="http_error", status_code=resp.status_code)

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        observe_web_ingest(
            outcome="http_error",
            duration_seconds=time.monotonic() - started,
        )
        return IngestResult(url=url, status="http_error")

    if not isinstance(body, dict) or not body.get("success"):
        observe_web_ingest(
            outcome="http_error",
            duration_seconds=time.monotonic() - started,
        )
        return IngestResult(url=url, status="http_error")

    data = body.get("data") or {}
    markdown = data.get("markdown") or ""
    metadata = data.get("metadata") or {}
    title = str(metadata.get("title") or "")
    status_code = int(metadata.get("statusCode") or 0)

    if not markdown.strip():
        observe_web_ingest(
            outcome="empty",
            duration_seconds=time.monotonic() - started,
        )
        return IngestResult(
            url=url, status="empty", title=title, status_code=status_code,
        )

    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_WEB_PAGE_BYTES:
        observe_web_ingest(
            outcome="http_error",
            duration_seconds=time.monotonic() - started,
        )
        return IngestResult(url=url, status="http_error", title=title)

    # Prepend a small YAML-style frontmatter so the source URL is
    # discoverable from the markdown alone (chonkie's frontmatter
    # stripper in scripts/measure_chunking_hit_rate.py will drop it
    # before measurement).
    frontmatter = (
        f"---\nsource_url: {url}\ntitle: {title}\nstatus_code: {status_code}\n---\n\n"
    )
    target.write_text(frontmatter + markdown)

    observe_web_ingest(
        outcome="success",
        duration_seconds=time.monotonic() - started,
    )
    return IngestResult(
        url=url,
        status="success",
        path=target,
        title=title,
        status_code=status_code,
        bytes_written=len(encoded),
    )
