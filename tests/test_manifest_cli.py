"""Tests for the orchestrator CLI (Phase E of Phase 1.5).

All tests invoke the CLI via subprocess so the actual entry point is exercised.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from manifest import write_campaign_merkle, write_run_manifest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_CWD = _REPO_ROOT
_ENV = {**os.environ, "PYTHONPATH": _REPO_ROOT}


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cli.main", *args],
        capture_output=True,
        text=True,
        cwd=_CWD,
        env=_ENV,
    )


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _make_run(projects_root: Path, project_name: str, run_id: str, content: bytes) -> Path:
    """Create a run dir with one file and a written manifest. Returns run_dir."""
    run_dir = projects_root / project_name / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write(run_dir / "output.txt", content)
    write_run_manifest(run_dir, run_id=run_id)
    return run_dir


# ---------------------------------------------------------------------------
# verify-run tests
# ---------------------------------------------------------------------------


def test_cli_verify_run_ok(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    _make_run(projects_dir, "proj-a", "run-ok123", b"some results")

    result = _run_cli(
        "verify-run", "run-ok123",
        "--projects-dir", str(projects_dir),
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert "run-ok123" in result.stdout


def test_cli_verify_run_corrupted(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    run_dir = _make_run(projects_dir, "proj-b", "run-corrupt", b"original data")

    # Tamper with the tracked file after the manifest was written
    tampered = run_dir / "output.txt"
    tampered.write_bytes(b"tampered data")

    result = _run_cli(
        "verify-run", "run-corrupt",
        "--projects-dir", str(projects_dir),
    )

    assert result.returncode == 1
    assert "FAIL" in result.stderr
    # The tampered file's name should appear in the mismatch report
    assert "output.txt" in result.stderr


def test_cli_verify_run_not_found(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    result = _run_cli(
        "verify-run", "nonexistent-run-id",
        "--projects-dir", str(projects_dir),
    )

    assert result.returncode == 1
    assert "not found" in result.stderr


def test_cli_verify_run_ambiguous(tmp_path: Path) -> None:
    """Same run_id under two different project dirs — should error out."""
    projects_dir = tmp_path / "projects"
    _make_run(projects_dir, "proj-x", "run-dupe", b"data x")
    _make_run(projects_dir, "proj-y", "run-dupe", b"data y")

    result = _run_cli(
        "verify-run", "run-dupe",
        "--projects-dir", str(projects_dir),
    )

    assert result.returncode == 1
    # Should mention that multiple matches were found
    assert "multiple" in result.stderr or "matches" in result.stderr


# ---------------------------------------------------------------------------
# verify-campaign tests
# ---------------------------------------------------------------------------


def test_cli_verify_campaign_ok(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    campaigns_dir = tmp_path / "campaigns"

    campaign_id = "camp-ok"
    campaign_dir = campaigns_dir / campaign_id
    campaign_dir.mkdir(parents=True)

    run_dir_a = _make_run(projects_dir, "proj-z", "run-1111", b"data_1")
    run_dir_b = _make_run(projects_dir, "proj-z", "run-2222", b"data_2")

    run_dirs = [
        ("run-1111", "proj-z", run_dir_a),
        ("run-2222", "proj-z", run_dir_b),
    ]
    write_campaign_merkle(campaign_dir, run_dirs)

    result = _run_cli(
        "verify-campaign", campaign_id,
        "--campaigns-dir", str(campaigns_dir),
        "--projects-dir", str(projects_dir),
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert campaign_id in result.stdout


def test_cli_verify_campaign_corrupted(tmp_path: Path) -> None:
    projects_dir = tmp_path / "projects"
    campaigns_dir = tmp_path / "campaigns"

    campaign_id = "camp-corrupt"
    campaign_dir = campaigns_dir / campaign_id
    campaign_dir.mkdir(parents=True)

    run_dir_a = _make_run(projects_dir, "proj-c", "run-aaaa", b"clean_data")
    run_dir_b = _make_run(projects_dir, "proj-c", "run-bbbb", b"other_data")

    run_dirs = [
        ("run-aaaa", "proj-c", run_dir_a),
        ("run-bbbb", "proj-c", run_dir_b),
    ]
    write_campaign_merkle(campaign_dir, run_dirs)

    # Tamper with run-aaaa's file and re-write its manifest so the manifest
    # sha256 stored in merkle.json no longer matches the on-disk manifest.
    (run_dir_a / "output.txt").write_bytes(b"tampered_data")
    write_run_manifest(run_dir_a, run_id="run-aaaa")

    result = _run_cli(
        "verify-campaign", campaign_id,
        "--campaigns-dir", str(campaigns_dir),
        "--projects-dir", str(projects_dir),
    )

    assert result.returncode == 1
    assert "FAIL" in result.stderr
    assert "run-aaaa" in result.stderr


def test_cli_verify_campaign_not_found(tmp_path: Path) -> None:
    campaigns_dir = tmp_path / "campaigns"
    campaigns_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    result = _run_cli(
        "verify-campaign", "nonexistent-campaign",
        "--campaigns-dir", str(campaigns_dir),
        "--projects-dir", str(projects_dir),
    )

    assert result.returncode == 1
    assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# Argparse smoke tests
# ---------------------------------------------------------------------------


def test_cli_no_args_shows_help() -> None:
    """Missing subcommand should cause argparse to exit with code 2."""
    result = _run_cli()
    assert result.returncode == 2
