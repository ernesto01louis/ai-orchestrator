"""Phase 3.3 NoteDiscovery REST client.

Talks to the NoteDiscovery FastAPI app at the configured base URL
(default ``http://192.168.2.203:8010``). Used by the planner agent
to ground campaign proposals in the operator's existing notes.

The original Phase 3.3 plan envisioned an MCP wrapper around
NoteDiscovery; the live container exposes a plain REST API instead,
so this module is a thin HTTP client built on ``requests`` (already
a project dep) rather than ``mcp.client.streamable_http``. See
[docs/NOTEDISCOVERY.md] for the full contract.

All public functions are best-effort and fail-open: a missing
NoteDiscovery never blocks a campaign, just degrades the planner to
its existing memory stack.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import requests

from core.config import (
    NOTEDISCOVERY_BASE_URL,
    NOTEDISCOVERY_ENABLED,
    NOTEDISCOVERY_TIMEOUT_SECONDS,
    NOTEDISCOVERY_TOP_K,
)

_HIGHLIGHT_RE = re.compile(r"<mark[^>]*>(.*?)</mark>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Note:
    """A single NoteDiscovery search result.

    ``snippet`` is the concatenated context from all matches in the
    note, with NoteDiscovery's ``<mark class=...>`` HTML stripped.
    """

    name: str
    path: str
    folder: str
    snippet: str


def _strip_marks(text: str) -> str:
    """Drop NoteDiscovery's ``<mark class="search-highlight">…</mark>``
    HTML used for hit-highlighting in match contexts."""
    return _HIGHLIGHT_RE.sub(r"\1", text or "")


def _api_key_header() -> dict[str, str]:
    """Optional X-API-Key header from ``.env``. NoteDiscovery 0.19.1
    advertises auth in OpenAPI but doesn't enforce it; we send the
    header anyway so future tightening doesn't break us."""
    key = os.environ.get("NOTEDISCOVERY_API_KEY", "").strip()
    return {"X-API-Key": key} if key else {}


def is_enabled() -> bool:
    """Three-condition gate (matches Phase 2.5 SkyPilot pattern):

      1. ``note_discovery.enabled = true`` in config
      2. ``NOTEDISCOVERY_BASE_URL`` non-empty
      3. ``requests`` importable (always true today, but kept symmetric
         for future swap-in of an alternate transport)

    Used by the planner before issuing any HTTP — when False, the
    research step short-circuits without touching the network.
    """
    return bool(NOTEDISCOVERY_ENABLED) and bool(NOTEDISCOVERY_BASE_URL)


def healthcheck() -> bool:
    """Cheap GET /health probe. Returns True iff the service is
    reachable and reports ``status == "healthy"``. Never raises.

    Called from ``app.py:_lifespan`` at startup to surface a warning
    if the operator flipped ``enabled=true`` but the container is
    unreachable; never fatal.
    """
    if not is_enabled():
        return False
    url = NOTEDISCOVERY_BASE_URL.rstrip("/") + "/health"
    try:
        resp = requests.get(
            url,
            timeout=min(5, NOTEDISCOVERY_TIMEOUT_SECONDS),
            headers=_api_key_header(),
        )
        if resp.status_code != 200:
            return False
        body = resp.json()
        return isinstance(body, dict) and body.get("status") == "healthy"
    except (requests.RequestException, ValueError, OSError):
        return False


def search_notes(query: str, *, top_k: int | None = None) -> list[Note]:
    """Search the NoteDiscovery vault for notes matching ``query``.

    Returns up to ``top_k`` (default ``NOTEDISCOVERY_TOP_K``) results
    or an empty list on any failure. Never raises — a wedged
    NoteDiscovery never breaks a planner call.
    """
    import time

    from core.metrics import observe_note_discovery_query

    if not is_enabled() or not query:
        observe_note_discovery_query("disabled", 0.0)
        return []

    limit = int(top_k) if top_k is not None else NOTEDISCOVERY_TOP_K
    url = NOTEDISCOVERY_BASE_URL.rstrip("/") + "/api/search"

    started = time.monotonic()
    try:
        resp = requests.get(
            url,
            params={"q": str(query), "limit": str(limit)},
            timeout=NOTEDISCOVERY_TIMEOUT_SECONDS,
            headers=_api_key_header(),
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError, OSError):
        observe_note_discovery_query("failure", time.monotonic() - started)
        return []

    if not isinstance(data, dict):
        observe_note_discovery_query("failure", time.monotonic() - started)
        return []

    out: list[Note] = []
    for raw in (data.get("results") or [])[:limit]:
        if not isinstance(raw, dict):
            continue
        matches = raw.get("matches") or []
        contexts = []
        for m in matches:
            if isinstance(m, dict):
                ctx = _strip_marks(str(m.get("context", "")))
                if ctx:
                    contexts.append(ctx)
        snippet = " … ".join(contexts) if contexts else ""
        out.append(Note(
            name=str(raw.get("name", "")),
            path=str(raw.get("path", "")),
            folder=str(raw.get("folder", "")),
            snippet=snippet,
        ))
    observe_note_discovery_query("success" if out else "empty", time.monotonic() - started)
    return out
