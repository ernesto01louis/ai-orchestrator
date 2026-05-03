"""End-to-end test for the evidence-bundle pipeline.

Drives ``build_bundle()`` against a synthetic campaign in memory,
asserts the on-disk crate has the right shape, the standalone verifier
exits 0, and tampering flips it to exit 1.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from core.campaign import Campaign, CampaignRun, CampaignTemplate
from core.paths import REPO_ROOT
from evidence.builder import _BundleBuilder
from evidence.signing import SigningKey
from evidence.verify import main as verify_main

pytestmark = pytest.mark.inprocess


_CAMPAIGN_ID = "evid-e2e-test"


@pytest.fixture
def campaign(tmp_path) -> Campaign:
    return Campaign(
        id=_CAMPAIGN_ID,
        name="evid-e2e",
        hypothesis="this campaign verifies cleanly post-build",
        description="end-to-end test fixture",
        template=CampaignTemplate(
            project_name="p", prompt="go", planner_model="m",
            generator_models=["m"], judge_model="m", deploy_target="pi-1",
        ),
        params={"seed": [1, 2]},
        status="completed",
        runs=[
            CampaignRun(run_id="ra", params={"seed": 1}, status="completed", score=7.0),
            CampaignRun(run_id="rb", params={"seed": 2}, status="completed", score=8.5),
        ],
        created_at="2026-05-03T10:00:00",
        updated_at="2026-05-03T10:30:00",
    )


@pytest.fixture
def crate_dir() -> Path:
    """Repo-relative crate dir; cleaned up post-test."""
    target = REPO_ROOT / "campaigns" / _CAMPAIGN_ID
    if target.exists():
        shutil.rmtree(target)
    yield target
    if target.exists():
        shutil.rmtree(target)


def test_build_bundle_writes_complete_crate(campaign, crate_dir):
    key = SigningKey.generate()
    bundle = _BundleBuilder(
        campaign=campaign, crate_dir=crate_dir, signing_key=key,
    ).build()

    # All the canonical files must land
    for name in (
        "evidence.json", "ro-crate-metadata.json",
        "manifest.json", "manifest.json.dsse",
        "public.key", "README.md",
        "checklists/reforms.md", "checklists/neurips.md",
        "datasheets/prompt_corpus.md",
    ):
        assert (crate_dir / name).exists(), f"missing {name}"

    # One model card per LLM target
    cards = list((crate_dir / "model_cards").glob("*.md"))
    assert len(cards) >= 1

    # Schema-required REFORMS items: 32; NeurIPS Q4-Q8: 5
    assert len(bundle.reforms_responses) == 32
    assert len(bundle.neurips_responses) == 5

    # All five builtin calculators ran
    assert {c.kind for c in bundle.calculators} == {
        "statistical_summary",
        "lineage",
        "compute_resources",
        "code_fingerprint",
        "hardware_fingerprint",
    }


def test_built_crate_verifies_cleanly(campaign, crate_dir):
    key = SigningKey.generate()
    _BundleBuilder(campaign=campaign, crate_dir=crate_dir, signing_key=key).build()
    sys.argv = ["verify", "--crate-dir", str(crate_dir)]
    assert verify_main() == 0


def test_tampered_artifact_fails_verification(campaign, crate_dir):
    """Modifying any file in the crate breaks the manifest digest."""
    key = SigningKey.generate()
    _BundleBuilder(campaign=campaign, crate_dir=crate_dir, signing_key=key).build()
    readme = crate_dir / "README.md"
    readme.write_text(readme.read_text() + "\nTAMPERED")

    sys.argv = ["verify", "--crate-dir", str(crate_dir)]
    assert verify_main() != 0


def test_tampered_signature_fails_verification(campaign, crate_dir):
    """Modifying manifest.json.dsse breaks the DSSE signature check."""
    key = SigningKey.generate()
    _BundleBuilder(campaign=campaign, crate_dir=crate_dir, signing_key=key).build()
    dsse = crate_dir / "manifest.json.dsse"
    raw = dsse.read_text()
    # Flip a single character inside the signature payload to corrupt it.
    bad = raw.replace('"sig":"', '"sig":"A', 1)
    dsse.write_text(bad)

    sys.argv = ["verify", "--crate-dir", str(crate_dir)]
    assert verify_main() != 0


def test_reforms_has_hypothesis_in_section_1_1(campaign, crate_dir):
    """REFORMS §1.1 must auto-fill from the campaign's required hypothesis."""
    key = SigningKey.generate()
    bundle = _BundleBuilder(
        campaign=campaign, crate_dir=crate_dir, signing_key=key,
    ).build()
    assert "this campaign verifies cleanly post-build" in bundle.reforms_responses["1.1"]
