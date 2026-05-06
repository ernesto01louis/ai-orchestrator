"""Tests for Phase 1.5 Phase B: manifest.write_run_manifest hook in run_orchestration.

Verifies that:
- A successful run writes manifest.json to the run_dir and sets manifest_status="ok".
- A manifest write failure does not fail the run; manifest_status="skipped" is set.

Uses the same in-process flow invocation pattern as test_orchestrate_flow.py:
run_orchestration(...) directly (Prefect test harness activated by conftest.py).
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from core.runtime import RUN_STATUS
from orchestration import OrchestrateRequest, run_orchestration

# ---------------------------------------------------------------------------
# Shared test data (mirrors test_orchestrate_flow.py)
# ---------------------------------------------------------------------------

_VALID_PLAN = {
    "language": "python",
    "project_type": "script",
    "execution_mode": "generate",
    "port": 0,
    "entrypoint": "main.py",
    "files": {"main.py": "print('hi')"},
    "dependencies": [],
    "steps": [],
    "approach": "print hello",
}

_VALID_CANDIDATE = {
    "model": "m1",
    "files": {"main.py": "print('hi')"},
    "score": 9,
    "judge": {"feedback": "looks good"},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_run_status():
    RUN_STATUS.clear()
    yield
    RUN_STATUS.clear()


@pytest.fixture
def tiny_request():
    return OrchestrateRequest(
        project_name="smoke",
        deploy_target="local",
        prompt="print hi",
        language="python",
        planner_model="planner",
        generator_models=["m1", "m2", "m3"],
        judge_model="judge",
    )


@pytest.fixture
def standard_patches():
    """Standard agent/helper patches that keep the flow fast and hermetic."""
    with ExitStack() as stack:
        stack.enter_context(patch("orchestration.planner_agent.fn", return_value=_VALID_PLAN))
        stack.enter_context(patch("orchestration.generate_candidate.fn", return_value=_VALID_CANDIDATE))
        stack.enter_context(
            patch(
                "orchestration.sandbox_execute",
                return_value={"returncode": 0, "stdout": "hi", "stderr": ""},
            )
        )
        stack.enter_context(
            patch(
                "orchestration.environment_inspector",
                return_value={"os": "Linux", "python": "python3", "node": "node", "arch": "x86_64"},
            )
        )
        stack.enter_context(patch("orchestration.gather_live_context", return_value=""))
        stack.enter_context(patch("orchestration.build_full_planner_context", return_value=""))
        stack.enter_context(patch("orchestration.notify_run_started"))
        stack.enter_context(patch("orchestration.notify_run_complete"))
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_manifest_written_on_completion(
    tmp_path: Path,
    tiny_request: OrchestrateRequest,
    mock_ollama,
    standard_patches,
    monkeypatch,
):
    """A successful run writes manifest.json and sets manifest_status='ok'."""
    run_id = "manifest-hook-test-1"

    # Redirect PROJECTS_DIR so artifacts land under tmp_path instead of the
    # real projects/ directory.
    monkeypatch.setattr("orchestration.PROJECTS_DIR", str(tmp_path))

    run_orchestration(tiny_request, run_id)

    # Run must have completed successfully.
    status = RUN_STATUS[run_id]
    assert status["completed"] is True
    assert status.get("error") is None

    # manifest_status must be "ok".
    assert status.get("manifest_status") == "ok", (
        f"Expected manifest_status='ok', got {status.get('manifest_status')!r}"
    )

    # manifest.json must exist in the run_dir.
    run_dir = tmp_path / tiny_request.project_name / "runs" / run_id
    manifest_path = run_dir / "manifest.json"
    assert manifest_path.exists(), f"manifest.json not found at {manifest_path}"

    # Validate manifest content.
    import json
    data = json.loads(manifest_path.read_text())
    assert data["version"] == 1
    assert data["run_id"] == run_id
    assert isinstance(data["files"], list)
    assert len(data["files"]) > 0, "manifest.files should list at least one artifact"

    # Every entry must have path and sha256.
    for entry in data["files"]:
        assert "path" in entry, f"entry missing 'path': {entry}"
        assert "sha256" in entry, f"entry missing 'sha256': {entry}"

    # manifest.json itself must NOT be listed (it's excluded by design).
    listed_paths = {e["path"] for e in data["files"]}
    assert "manifest.json" not in listed_paths, "manifest.json should not list itself"


def test_manifest_failure_does_not_fail_run(
    tmp_path: Path,
    tiny_request: OrchestrateRequest,
    mock_ollama,
    standard_patches,
    monkeypatch,
):
    """When write_run_manifest raises, the run still completes and manifest_status='skipped'."""
    run_id = "manifest-hook-test-2"

    monkeypatch.setattr("orchestration.PROJECTS_DIR", str(tmp_path))

    # Make write_run_manifest blow up.
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("orchestration.write_run_manifest", _boom)

    # Must not raise.
    run_orchestration(tiny_request, run_id)

    status = RUN_STATUS[run_id]

    # Run must still be marked completed.
    assert status["completed"] is True, "Run should complete even when manifest write fails"

    # manifest_status must be "skipped" (not "ok", not None).
    assert status.get("manifest_status") == "skipped", (
        f"Expected manifest_status='skipped', got {status.get('manifest_status')!r}"
    )
