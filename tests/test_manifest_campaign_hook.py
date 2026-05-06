"""Tests for Phase 1.5 Phase C: manifest.write_campaign_merkle hook in run_campaign.

Verifies that:
- A successful 2-run campaign writes merkle.json to campaign_dir and sets
  manifest_status="ok" in CAMPAIGN_STATUS.
- The written merkle.json is immediately verifiable via verify_campaign_merkle.
- Tampering with a per-run manifest.json causes verify_campaign_merkle to
  report "corrupted" with the affected run_id in mismatches.

Uses the same in-process flow invocation pattern as test_campaign_flow.py:
run_campaign(...) directly (Prefect test harness activated by conftest.py).
No `prefect_real` marker — runs in the default suite.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

import manifest as manifest_mod
from core.runtime import CAMPAIGN_STATUS, RUN_STATUS, _init_run_status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_campaign(tmp_campaign_dir: Path, tmp_projects_dir: Path) -> str:
    """Create a 2-combo campaign via api.routes.create_campaign and return campaign_id.

    Patches submit_campaign (no real Prefect submission) and validate_target.
    """
    from api.routes import create_campaign
    from core.campaign import CampaignCreate

    req = CampaignCreate(
        name="merkle-test",
        hypothesis="Campaign merkle Phase C test.",
        template={
            "project_name": "merkle-smoke",
            "deploy_target": "local",
            "prompt": "print {x}",
            "language": "python",
            "generator_models": ["m1"],
            "judge_model": "judge",
            "planner_model": "planner",
        },
        params={"x": [1, 2]},
    )

    with ExitStack() as stack:
        stack.enter_context(patch("api.routes.validate_target"))
        stack.enter_context(
            patch(
                "api.routes.submit_campaign",
                return_value={"campaign_id": "ignored", "flow_run_id": "fake-frid"},
            )
        )
        response = create_campaign(req)

    return response["campaign_id"]


def _make_run_orchestration_mock(tmp_projects_dir: Path, captured_run_ids: list):
    """Return a mock run_orchestration that:
    - records project in RUN_STATUS (simulating _init_run_status's project kwarg)
    - creates run_dir / manifest.json under tmp_projects_dir
    - appends the run_id to captured_run_ids
    """
    def _fake_run(req, run_id: str):
        project = req.project_name
        # Set project in RUN_STATUS (mirrors _init_run_status kwargs).
        _init_run_status(run_id, project=project, completed=True)
        # Create run_dir with a manifest.json so merkle can hash it.
        run_dir = tmp_projects_dir / project / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "score.txt").write_text("9\n")
        manifest_data = {
            "version": 1,
            "run_id": run_id,
            "files": [{"path": "score.txt", "sha256": "aabbcc"}],
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest_data))
        captured_run_ids.append(run_id)

    return _fake_run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_statuses():
    RUN_STATUS.clear()
    CAMPAIGN_STATUS.clear()
    yield
    RUN_STATUS.clear()
    CAMPAIGN_STATUS.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_campaign_merkle_written(tmp_path: Path):
    """A completed 2-run campaign writes merkle.json and sets manifest_status='ok'."""
    tmp_projects_dir = tmp_path / "projects"
    tmp_campaign_dir = tmp_path / "campaigns"
    tmp_campaign_dir.mkdir(parents=True, exist_ok=True)

    campaign_id = _make_campaign(tmp_campaign_dir, tmp_projects_dir)
    captured_run_ids: list = []

    fake_run = _make_run_orchestration_mock(tmp_projects_dir, captured_run_ids)

    with ExitStack() as stack:
        mock_ro = stack.enter_context(patch("orchestration.run_orchestration"))
        mock_ro.with_options.return_value = mock_ro
        mock_ro.side_effect = fake_run
        # Redirect campaign_dir (CAMPAIGN_TEMPLATES_DIR) and run dirs (PROJECTS_DIR).
        stack.enter_context(
            patch("orchestration.campaign.CAMPAIGN_TEMPLATES_DIR", tmp_campaign_dir)
        )
        stack.enter_context(
            patch("orchestration.campaign.PROJECTS_DIR", str(tmp_projects_dir))
        )
        # Suppress vault/evidence side-effects.
        stack.enter_context(patch("orchestration.campaign._safe_vault_write"))
        stack.enter_context(
            patch(
                "orchestration.campaign._safe_emit_evidence",
                **{"submit.return_value.result.return_value": None},
            )
        )
        from orchestration.campaign import run_campaign
        run_campaign(campaign_id)

    # Two runs must have been recorded.
    assert len(captured_run_ids) == 2, f"Expected 2 run_ids, got {captured_run_ids}"

    # manifest_status must be "ok".
    status = CAMPAIGN_STATUS.get(campaign_id, {})
    assert status.get("manifest_status") == "ok", (
        f"Expected manifest_status='ok', got {status.get('manifest_status')!r}"
    )

    # merkle.json must exist in campaign_dir.
    campaign_dir = tmp_campaign_dir / campaign_id
    merkle_path = campaign_dir / "merkle.json"
    assert merkle_path.exists(), f"merkle.json not found at {merkle_path}"

    # Validate merkle.json content.
    data = json.loads(merkle_path.read_text())
    assert data["version"] == 1
    assert data["campaign_id"] == campaign_id
    assert isinstance(data["runs"], list)
    assert len(data["runs"]) == 2, f"Expected 2 run entries, got {len(data['runs'])}"
    assert data["merkle_root"] != "", "merkle_root must be a non-empty hex string"

    # Each run entry must have run_id, project_name, manifest_sha256.
    stored_run_ids = {r["run_id"] for r in data["runs"]}
    assert stored_run_ids == set(captured_run_ids), (
        f"run_ids in merkle don't match captured: {stored_run_ids} != {set(captured_run_ids)}"
    )
    for entry in data["runs"]:
        assert entry["project_name"] == "merkle-smoke"
        assert len(entry["manifest_sha256"]) == 64, "manifest_sha256 must be a 64-char hex string"


def test_campaign_merkle_verifiable_immediately(tmp_path: Path):
    """merkle.json written by run_campaign passes verify_campaign_merkle immediately."""
    tmp_projects_dir = tmp_path / "projects"
    tmp_campaign_dir = tmp_path / "campaigns"
    tmp_campaign_dir.mkdir(parents=True, exist_ok=True)

    campaign_id = _make_campaign(tmp_campaign_dir, tmp_projects_dir)
    captured_run_ids: list = []

    fake_run = _make_run_orchestration_mock(tmp_projects_dir, captured_run_ids)

    with ExitStack() as stack:
        mock_ro = stack.enter_context(patch("orchestration.run_orchestration"))
        mock_ro.with_options.return_value = mock_ro
        mock_ro.side_effect = fake_run
        stack.enter_context(
            patch("orchestration.campaign.CAMPAIGN_TEMPLATES_DIR", tmp_campaign_dir)
        )
        stack.enter_context(
            patch("orchestration.campaign.PROJECTS_DIR", str(tmp_projects_dir))
        )
        stack.enter_context(patch("orchestration.campaign._safe_vault_write"))
        stack.enter_context(
            patch(
                "orchestration.campaign._safe_emit_evidence",
                **{"submit.return_value.result.return_value": None},
            )
        )
        from orchestration.campaign import run_campaign
        run_campaign(campaign_id)

    campaign_dir = tmp_campaign_dir / campaign_id
    result = manifest_mod.verify_campaign_merkle(campaign_dir, tmp_projects_dir)
    assert result.status == "ok", (
        f"verify_campaign_merkle returned {result.status!r}; mismatches={result.mismatches}"
    )


def test_campaign_merkle_detects_corrupted_run(tmp_path: Path):
    """Tampering with a run's manifest.json causes verify_campaign_merkle to report 'corrupted'."""
    tmp_projects_dir = tmp_path / "projects"
    tmp_campaign_dir = tmp_path / "campaigns"
    tmp_campaign_dir.mkdir(parents=True, exist_ok=True)

    campaign_id = _make_campaign(tmp_campaign_dir, tmp_projects_dir)
    captured_run_ids: list = []

    fake_run = _make_run_orchestration_mock(tmp_projects_dir, captured_run_ids)

    with ExitStack() as stack:
        mock_ro = stack.enter_context(patch("orchestration.run_orchestration"))
        mock_ro.with_options.return_value = mock_ro
        mock_ro.side_effect = fake_run
        stack.enter_context(
            patch("orchestration.campaign.CAMPAIGN_TEMPLATES_DIR", tmp_campaign_dir)
        )
        stack.enter_context(
            patch("orchestration.campaign.PROJECTS_DIR", str(tmp_projects_dir))
        )
        stack.enter_context(patch("orchestration.campaign._safe_vault_write"))
        stack.enter_context(
            patch(
                "orchestration.campaign._safe_emit_evidence",
                **{"submit.return_value.result.return_value": None},
            )
        )
        from orchestration.campaign import run_campaign
        run_campaign(campaign_id)

    assert len(captured_run_ids) == 2, f"Expected 2 run_ids, got {captured_run_ids}"

    # Tamper with the first run's manifest.json by appending a byte.
    # This changes the file's sha256 without touching merkle.json's stored leaf hashes.
    tampered_run_id = captured_run_ids[0]
    manifest_path = (
        tmp_projects_dir / "merkle-smoke" / "runs" / tampered_run_id / "manifest.json"
    )
    assert manifest_path.exists(), f"manifest.json not found at {manifest_path}"
    original_bytes = manifest_path.read_bytes()
    manifest_path.write_bytes(original_bytes + b"\x00")  # append one null byte

    # verify_campaign_merkle must detect the tampered leaf.
    campaign_dir = tmp_campaign_dir / campaign_id
    result = manifest_mod.verify_campaign_merkle(campaign_dir, tmp_projects_dir)
    assert result.status == "corrupted", (
        f"Expected 'corrupted', got {result.status!r}; mismatches={result.mismatches}"
    )
    assert tampered_run_id in result.mismatches, (
        f"Tampered run_id {tampered_run_id!r} not in mismatches: {result.mismatches}"
    )
