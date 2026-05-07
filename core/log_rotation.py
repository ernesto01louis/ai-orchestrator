"""Per-run log rotation for LOG_DIR.

The orchestrator writes one log file per run (LOG_DIR/{run_id}.log). Over
time these accumulate; this module gzips files older than `gzip_after_days`
and deletes gzipped files older than `delete_after_days`.  Pure function
with injectable `now` for testing.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class RotationSummary:
    gzipped: list[str] = field(default_factory=list)   # paths of newly gzipped files
    deleted: list[str] = field(default_factory=list)   # paths of deleted .log.gz files
    skipped: list[str] = field(default_factory=list)   # paths skipped (e.g., still open, error)
    errors: list[str] = field(default_factory=list)    # human-readable error strings


def rotate_logs(
    log_dir: "str | Path",
    *,
    gzip_after_days: int = 1,
    delete_after_days: int = 90,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> RotationSummary:
    """Gzip .log files older than *gzip_after_days* and delete .log.gz files
    older than *delete_after_days*.

    Parameters
    ----------
    log_dir:
        Directory containing per-run log files.
    gzip_after_days:
        Files whose mtime is older than this many days will be gzipped.
    delete_after_days:
        Gzipped files whose mtime is older than this many days will be deleted.
    now:
        Reference timestamp.  Defaults to ``datetime.utcnow()``.  Inject a
        fixed value in tests to make assertions deterministic.
    dry_run:
        When *True*, walk the same files but make no filesystem changes.
        Files that *would* be gzipped/deleted are reported in the summary.
    """
    summary = RotationSummary()

    if now is None:
        now = datetime.utcnow()

    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        summary.errors.append(f"{log_dir}: directory not found or not accessible")
        return summary

    gzip_cutoff = now - timedelta(days=gzip_after_days)
    delete_cutoff = now - timedelta(days=delete_after_days)

    # ── Phase 1: gzip old .log files ─────────────────────────────────────────
    for log_path in sorted(log_dir.glob("*.log")):
        if not log_path.is_file():
            continue
        if log_path.is_symlink():
            summary.skipped.append(str(log_path))
            continue
        try:
            mtime = datetime.utcfromtimestamp(log_path.stat().st_mtime)
        except OSError as exc:
            summary.errors.append(f"{log_path}: {exc}")
            continue
        if mtime >= gzip_cutoff:
            continue  # too recent — leave it alone

        gz_path = log_path.with_suffix(".log.gz")
        if dry_run:
            summary.gzipped.append(str(log_path))
            continue
        try:
            with log_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.unlink(log_path)
            summary.gzipped.append(str(gz_path))
        except OSError as exc:
            summary.errors.append(f"{log_path}: {exc}")

    # ── Phase 2: delete old .log.gz files ────────────────────────────────────
    for gz_path in sorted(log_dir.glob("*.log.gz")):
        if not gz_path.is_file():
            continue
        if gz_path.is_symlink():
            summary.skipped.append(str(gz_path))
            continue
        try:
            mtime = datetime.utcfromtimestamp(gz_path.stat().st_mtime)
        except OSError as exc:
            summary.errors.append(f"{gz_path}: {exc}")
            continue
        if mtime >= delete_cutoff:
            continue  # not old enough yet

        if dry_run:
            summary.deleted.append(str(gz_path))
            continue
        try:
            os.unlink(gz_path)
            summary.deleted.append(str(gz_path))
        except OSError as exc:
            summary.errors.append(f"{gz_path}: {exc}")

    return summary


def start_rotation_daemon(
    log_dir: "str | Path",
    *,
    interval_seconds: int = 86400,  # 24 hours
    gzip_after_days: int = 1,
    delete_after_days: int = 90,
) -> threading.Thread:
    """Start a daemon thread that runs :func:`rotate_logs` once at startup,
    then every *interval_seconds*.

    Returns the thread handle (callers don't normally need it; exposed for
    tests).  Errors are caught and printed to *stderr* so a transient FS
    error never kills the thread.
    """
    def _loop() -> None:
        while True:
            try:
                rotate_logs(
                    log_dir,
                    gzip_after_days=gzip_after_days,
                    delete_after_days=delete_after_days,
                )
            except Exception as exc:  # pragma: no cover
                print(f"WARNING: log rotation failed: {exc}", file=sys.stderr)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True, name="log-rotation")
    thread.start()
    return thread
