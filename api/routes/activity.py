"""Activity timeline route — a chronological feed of meaningful events.

Powers the operator console's Timeline page. Aggregates timestamped
events at query time (no event store, no new persistence) from three
sources already on disk:

  * run completions   — memory/run_index.json
  * campaign lifecycle — memory/campaigns.json
  * git commits        — `git log` on the orchestrator repo

Each event is normalised to {id, type, timestamp, title, details,
tags, link} and the feed is returned newest-first.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from core.paths import MEMORY_DIR, REPO_ROOT
from memory_pkg import load_campaigns

router = APIRouter()

_RUN_INDEX = Path(MEMORY_DIR) / "run_index.json"


def _parse_ts(raw: str | None) -> datetime | None:
    """Tolerant ISO-8601 parse. Returns an aware datetime or None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_events(cutoff: datetime) -> list[dict[str, Any]]:
    """Run completions from memory/run_index.json."""
    try:
        index = json.loads(_RUN_INDEX.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    events: list[dict[str, Any]] = []
    for run_id, info in index.items():
        if not isinstance(info, dict):
            continue
        ts = _parse_ts(info.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        phase = info.get("phase", "?")
        project = info.get("project", "?")
        has_error = bool(info.get("has_error"))
        score = info.get("score")
        events.append({
            "id": f"run:{run_id}",
            "type": "run",
            "timestamp": ts.isoformat(),
            "title": f"Run {run_id[:8]} — {phase}",
            "details": f"project {project}, target {info.get('target', '?')}"
                       + (f", score {score}" if score is not None else ""),
            "tags": [t for t in (project, phase, "error" if has_error else "") if t],
            "link": {"path": "/runs", "id": run_id},
        })
    return events


def _campaign_events(cutoff: datetime) -> list[dict[str, Any]]:
    """Campaign created / completed lifecycle events."""
    try:
        campaigns = load_campaigns()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for cid, camp in campaigns.items():
        if not isinstance(camp, dict):
            continue
        name = camp.get("name", cid[:8])
        n_runs = len(camp.get("runs", []))
        created = _parse_ts(camp.get("created_at"))
        if created is not None and created >= cutoff:
            events.append({
                "id": f"campaign-created:{cid}",
                "type": "campaign",
                "timestamp": created.isoformat(),
                "title": f"Campaign '{name}' created",
                "details": f"{n_runs} run(s), status {camp.get('status', '?')}",
                "tags": ["campaign", "created"],
                "link": {"path": "/campaigns", "id": cid},
            })
        completed = _parse_ts(camp.get("completed_at"))
        if completed is not None and completed >= cutoff:
            events.append({
                "id": f"campaign-completed:{cid}",
                "type": "campaign",
                "timestamp": completed.isoformat(),
                "title": f"Campaign '{name}' completed",
                "details": f"{n_runs} run(s), status {camp.get('status', '?')}",
                "tags": ["campaign", "completed", str(camp.get("status", ""))],
                "link": {"path": "/campaigns", "id": cid},
            })
    return events


def _git_events(cutoff: datetime, limit: int) -> list[dict[str, Any]]:
    """Recent git commits on the orchestrator repo."""
    try:
        # %H sha, %aI author-date ISO, %s subject — unit-separator delimited.
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log",
             f"-n{limit}", "--pretty=format:%H%x1f%aI%x1f%s"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    events: list[dict[str, Any]] = []
    for line in out.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, raw_ts, subject = parts
        ts = _parse_ts(raw_ts)
        if ts is None or ts < cutoff:
            continue
        # conventional-commit type prefix, e.g. "feat(x): ..." -> "feat"
        ctype = subject.split("(")[0].split(":")[0].strip() if ":" in subject else "commit"
        events.append({
            "id": f"git:{sha}",
            "type": "git",
            "timestamp": ts.isoformat(),
            "title": subject,
            "details": f"commit {sha[:10]}",
            "tags": ["commit", ctype],
            "link": None,
        })
    return events


@router.get("/activity")
def get_activity(limit: int = 200, hours: int = 168) -> dict[str, Any]:
    """Chronological activity feed for the operator console Timeline page.

    Args:
        limit: max events returned (newest-first).
        hours: lookback window (default 168 = 7 days).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    events: list[dict[str, Any]] = []
    events += _run_events(cutoff)
    events += _campaign_events(cutoff)
    events += _git_events(cutoff, limit)
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return {"events": events[: max(1, limit)], "window_hours": hours}
