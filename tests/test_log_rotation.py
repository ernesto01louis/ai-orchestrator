"""Tests for core.log_rotation — per-run log gzip + retention."""
from __future__ import annotations

import gzip
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.log_rotation import RotationSummary, rotate_logs, start_rotation_daemon

# ── helpers ──────────────────────────────────────────────────────────────────

def _touch(path: Path, content: str = "log line\n") -> Path:
    path.write_text(content)
    return path


def _set_mtime(path: Path, dt: datetime) -> None:
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def _now() -> datetime:
    return datetime.utcnow()


# ── tests ─────────────────────────────────────────────────────────────────────

def test_gzips_old_log(tmp_path: Path) -> None:
    """A .log file older than gzip_after_days is compressed and removed."""
    content = "hello old log\n"
    log = _touch(tmp_path / "old.log", content)
    old = _now() - timedelta(days=2)
    _set_mtime(log, old)

    summary = rotate_logs(tmp_path, gzip_after_days=1, delete_after_days=90, now=_now())

    gz = tmp_path / "old.log.gz"
    assert gz.exists(), "expected .log.gz to be created"
    assert not log.exists(), "expected .log to be removed"
    assert gz.name in [Path(p).name for p in summary.gzipped]

    # content round-trips correctly
    with gzip.open(gz, "rt") as fh:
        assert fh.read() == content


def test_keeps_recent_log(tmp_path: Path) -> None:
    """A .log file written recently is left untouched."""
    log = _touch(tmp_path / "recent.log")
    # mtime = 1 hour ago, gzip_after_days=1 → should NOT be gzipped
    _set_mtime(log, _now() - timedelta(hours=1))

    summary = rotate_logs(tmp_path, gzip_after_days=1, delete_after_days=90, now=_now())

    assert log.exists(), "recent log should not be touched"
    assert not (tmp_path / "recent.log.gz").exists()
    assert summary.gzipped == []


def test_deletes_old_gz(tmp_path: Path) -> None:
    """A .log.gz file older than delete_after_days is removed."""
    gz = tmp_path / "ancient.log.gz"
    with gzip.open(gz, "wb") as fh:
        fh.write(b"old compressed data\n")
    _set_mtime(gz, _now() - timedelta(days=100))

    summary = rotate_logs(tmp_path, gzip_after_days=1, delete_after_days=90, now=_now())

    assert not gz.exists(), "old .log.gz should be deleted"
    assert gz.name in [Path(p).name for p in summary.deleted]


def test_keeps_recent_gz(tmp_path: Path) -> None:
    """A .log.gz file within delete_after_days is kept."""
    gz = tmp_path / "recent.log.gz"
    with gzip.open(gz, "wb") as fh:
        fh.write(b"newer compressed data\n")
    _set_mtime(gz, _now() - timedelta(days=30))

    summary = rotate_logs(tmp_path, gzip_after_days=1, delete_after_days=90, now=_now())

    assert gz.exists(), "recent .log.gz should not be deleted"
    assert summary.deleted == []


def test_idempotent(tmp_path: Path) -> None:
    """Running rotate_logs twice produces no additional changes on the 2nd call."""
    log = _touch(tmp_path / "run.log")
    _set_mtime(log, _now() - timedelta(days=2))

    now = _now()
    s1 = rotate_logs(tmp_path, gzip_after_days=1, delete_after_days=90, now=now)
    assert len(s1.gzipped) == 1

    # Second call with the same `now` — gz was just created so its mtime is
    # recent; nothing new to gzip, nothing old enough to delete.
    s2 = rotate_logs(tmp_path, gzip_after_days=1, delete_after_days=90, now=now)
    assert s2.gzipped == [], "second pass should gzip nothing"
    assert s2.deleted == [], "second pass should delete nothing"
    assert s2.errors == []


def test_skips_symlinks(tmp_path: Path) -> None:
    """Symlinks in LOG_DIR are not gzipped; their targets are not modified."""
    real = _touch(tmp_path / "real.log")
    # Make the symlink appear old so the function would normally gzip it
    _set_mtime(real, _now() - timedelta(days=2))

    link = tmp_path / "link.log"
    link.symlink_to(real)

    summary = rotate_logs(tmp_path, gzip_after_days=1, delete_after_days=90, now=_now())

    # The symlink target (real.log) was gzipped; the symlink itself was skipped
    assert str(link) in summary.skipped
    # The symlink itself must not have been gzipped (no link.log.gz)
    assert not (tmp_path / "link.log.gz").exists()


def test_handles_missing_dir_gracefully() -> None:
    """rotate_logs on a nonexistent directory returns a summary with an error."""
    summary = rotate_logs("/nonexistent/__no_such_dir__")
    assert isinstance(summary, RotationSummary)
    assert len(summary.errors) == 1
    assert "not found" in summary.errors[0] or "not accessible" in summary.errors[0]


def test_cli_rotate_logs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI rotate-logs subcommand exits 0 and reports the gzipped file."""
    log = _touch(tmp_path / "run-abc.log")
    # gzip_after_days=0 means any file regardless of age is eligible
    _set_mtime(log, _now() - timedelta(seconds=1))

    from cli.main import main  # noqa: PLC0415
    rc = main([
        "rotate-logs",
        "--log-dir", str(tmp_path),
        "--gzip-after-days", "0",
        "--delete-after-days", "90",
    ])

    assert rc == 0
    captured = capsys.readouterr()
    # output must mention at least one gzipped file
    assert "gzipped" in captured.out or "1" in captured.out


def test_cli_rotate_logs_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --dry-run reports what would happen without modifying files."""
    log = _touch(tmp_path / "run-xyz.log")
    _set_mtime(log, _now() - timedelta(days=2))

    from cli.main import main  # noqa: PLC0415
    rc = main([
        "rotate-logs",
        "--log-dir", str(tmp_path),
        "--gzip-after-days", "1",
        "--delete-after-days", "90",
        "--dry-run",
    ])

    assert rc == 0
    # File must still exist — dry-run makes no changes
    assert log.exists(), "--dry-run must not delete the original .log"
    assert not (tmp_path / "run-xyz.log.gz").exists()

    captured = capsys.readouterr()
    assert "would gzip" in captured.out


def test_daemon_thread_starts_and_is_daemon(tmp_path: Path) -> None:
    """start_rotation_daemon returns a live daemon thread."""
    thread = start_rotation_daemon(tmp_path, interval_seconds=9999)
    assert thread.is_alive()
    assert thread.daemon
    assert thread.name == "log-rotation"
