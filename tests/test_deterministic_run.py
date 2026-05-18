"""Tests for deterministic-run evidence registration (Feature A).

A deterministic run executes a fixed recipe with no LLM pipeline.
``core.deterministic_run.register_deterministic_run`` writes it into the
on-disk layout the evidence builder expects; ``build_bundle`` must then
produce a full, verifiable bundle stamped ``provenance_mode='deterministic'``
with an intentionally-empty ``llm_calls[]``.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from core.deterministic_run import register_deterministic_run
from core.paths import PROJECTS_DIR, REPO_ROOT
from evidence.builder import build_bundle
from evidence.signing import SigningKey
from evidence.verify import main as verify_main
from memory_pkg import load_campaigns, save_campaigns

pytestmark = pytest.mark.inprocess

_CID = "det-run-test"
_PROJECT = "det-run-test-project"


@pytest.fixture
def cleanup():
    """Drop the test campaign, project dir and crate after each test."""
    yield
    campaigns = load_campaigns()
    if _CID in campaigns:
        del campaigns[_CID]
        save_campaigns(campaigns, changed_ids=[_CID])
    for p in (Path(PROJECTS_DIR) / _PROJECT, REPO_ROOT / "campaigns" / _CID):
        if p.exists():
            shutil.rmtree(p)


def _register(run_id: str, s_plus: int, returncode: int = 0) -> None:
    register_deterministic_run(
        campaign_id=_CID,
        campaign_name="deterministic run test",
        hypothesis="a deterministic run produces a verifiable bundle",
        project_name=_PROJECT,
        deploy_target="test-target",
        run_id=run_id,
        params={"s_plus": s_plus},
        recipe="blockMesh\nsimpleFoam\n",
        stdout="converged",
        stderr="",
        returncode=returncode,
        score=float(s_plus),
        duration_ms=1000,
        solver="OpenFOAM v2412",
    )


def test_register_writes_run_artifacts(cleanup):
    _register("run-a", 17)
    run_dir = Path(PROJECTS_DIR) / _PROJECT / "runs" / "run-a"
    for name in ("plan.json", "execution.json", "environment.json",
                 "score.txt", "prompt.txt", "files.json", "manifest.json"):
        assert (run_dir / name).exists(), f"missing {name}"
    assert (run_dir / "src" / "run.sh").exists()


def test_plan_json_is_stamped_deterministic(cleanup):
    import json
    _register("run-a", 17)
    plan = json.loads(
        (Path(PROJECTS_DIR) / _PROJECT / "runs" / "run-a" / "plan.json").read_text()
    )
    assert plan["provenance_mode"] == "deterministic"
    assert plan["language"] == "bash"


def test_register_creates_campaign_record(cleanup):
    _register("run-a", 17)
    campaigns = load_campaigns()
    assert _CID in campaigns
    assert len(campaigns[_CID]["runs"]) == 1
    assert campaigns[_CID]["runs"][0]["run_id"] == "run-a"


def test_second_run_extends_campaign(cleanup):
    _register("run-a", 17)
    _register("run-b", 20)
    campaigns = load_campaigns()
    runs = campaigns[_CID]["runs"]
    assert {r["run_id"] for r in runs} == {"run-a", "run-b"}
    # the campaign sweep grid accumulates each run's params
    assert sorted(campaigns[_CID]["params"]["s_plus"]) == [17, 20]


def test_bundle_marks_runs_deterministic(cleanup):
    _register("run-a", 17)
    bundle = build_bundle(_CID, signing_key=SigningKey.generate())
    assert len(bundle.runs) == 1
    assert bundle.runs[0].provenance_mode == "deterministic"
    # empty llm_calls is intentional for a deterministic run, not degraded
    assert bundle.runs[0].llm_calls == []


def test_compute_calculator_counts_deterministic_runs(cleanup):
    _register("run-a", 17)
    bundle = build_bundle(_CID, signing_key=SigningKey.generate())
    compute = next(c for c in bundle.calculators if c.kind == "compute_resources")
    assert compute.output["n_deterministic_runs"] == 1
    assert compute.output["n_llm_pipeline_runs"] == 0
    assert compute.output["llm_call_count"] == 0


def test_deterministic_bundle_verifies_clean(cleanup):
    _register("run-a", 17)
    build_bundle(_CID, signing_key=SigningKey.generate())
    sys.argv = ["verify", "--crate-dir", str(REPO_ROOT / "campaigns" / _CID)]
    assert verify_main() == 0
