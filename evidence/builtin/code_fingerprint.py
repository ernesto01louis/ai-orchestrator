"""Code-fingerprint calculator.

Augments the bundle's typed ``CodeFingerprint`` (git_remote, git_sha,
git_dirty, lock contents+sha) with the additional context a researcher
needs to identify a build state without re-resolving the SHA: branch
name, the most recent annotated tag, the HEAD commit message, and the
list of dirty files when the working tree is dirty.

Pure provenance — no domain logic. Fails open: any subprocess call
that errors yields a null in the output rather than crashing the
calculator.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.evidence import CalculatorResult
from core.paths import REPO_ROOT
from evidence.hookspecs import hookimpl

if TYPE_CHECKING:
    from core.campaign import Campaign
    from core.evidence import RunRecord


_CALCULATOR_ID = "ai_orchestrator.builtin.code_fingerprint:v1"
_OUTPUT_SCHEMA_VERSION = "1.0.0"


def _git(*args: str, cwd: Path = REPO_ROOT) -> str | None:
    """Run a git command; return stripped stdout or None on any error."""
    if shutil.which("git") is None:
        return None
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


@hookimpl
def compute_evidence(
    campaign: "Campaign", runs: "list[RunRecord]"
) -> list[CalculatorResult]:
    started = time.monotonic()

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    most_recent_tag = _git("describe", "--tags", "--abbrev=0")
    head_subject = _git("log", "-1", "--pretty=%s")
    head_author = _git("log", "-1", "--pretty=%an <%ae>")
    head_authored = _git("log", "-1", "--pretty=%aI")  # ISO-8601 strict
    dirty_status = _git("status", "--porcelain")
    dirty_files = (
        [line[3:] for line in dirty_status.splitlines() if line.strip()]
        if dirty_status
        else []
    )

    output = {
        "branch": branch,
        "most_recent_tag": most_recent_tag,
        "head_commit_subject": head_subject,
        "head_commit_author": head_author,
        "head_authored_at": head_authored,
        "dirty_files": dirty_files,
        "dirty_file_count": len(dirty_files),
    }

    duration_ms = int((time.monotonic() - started) * 1000)
    return [
        CalculatorResult(
            kind="code_fingerprint",
            calculator_id=_CALCULATOR_ID,
            schema_version=_OUTPUT_SCHEMA_VERSION,
            inputs={"repo_root": str(REPO_ROOT), "campaign_id": campaign.id},
            output=output,
            # Output depends on git working-tree state at call time, so
            # not deterministic across two runs unless the tree is identical.
            duration_ms=duration_ms,
            deterministic=False,
        )
    ]
