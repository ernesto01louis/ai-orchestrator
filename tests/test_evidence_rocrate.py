"""Tests for the RO-Crate 1.2 / WRROC mapper."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from core.evidence import (
    Artifact,
    CodeFingerprint,
    EvidenceBundle,
    HardwareFingerprint,
    LlmTarget,
    RunRecord,
)
from evidence.rocrate import from_rocrate, to_rocrate


@pytest.fixture
def bundle() -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="B1",
        campaign_id="C1",
        campaign_name="rocrate-test",
        created_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
        abstract="abstract",
        hypothesis="rocrate roundtrip is exact",
        code=CodeFingerprint(
            git_remote="https://github.com/x/y", git_sha="abc",
            git_dirty=False, requirements_lock="",
            requirements_lock_sha256="0" * 64,
        ),
        hardware=HardwareFingerprint(
            cpu_model="x", cpu_count=1, ram_gb=1.0, os="linux", kernel="6.x",
        ),
        llm_targets=[
            LlmTarget(
                role="judge", host="127.0.0.1:11434", model_name="qwen2.5:72b",
                model_digest="sha256-abc", model_size_bytes=42,
            ),
        ],
        runs=[
            RunRecord(
                run_id="r1", parameters={"seed": 1}, status="success",
                started_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 5, 3, 12, 1, 0, tzinfo=timezone.utc),
            ),
        ],
        artifacts=[
            Artifact(
                path="artifacts/r1/log.txt", sha256="1" * 64,
                content_type="text/plain", size_bytes=10, role="log",
            ),
        ],
    )


def test_to_rocrate_is_json_serializable(bundle: EvidenceBundle):
    """Whatever dict we emit MUST round-trip through json.dumps cleanly."""
    crate = to_rocrate(bundle)
    json.dumps(crate)


def test_rocrate_declares_both_profiles(bundle: EvidenceBundle):
    """conformsTo MUST list RO-Crate 1.2 AND the WRROC Provenance profile."""
    crate = to_rocrate(bundle)
    descriptor = next(
        e for e in crate["@graph"] if e.get("@id") == "ro-crate-metadata.json"
    )
    profiles = {p["@id"] for p in descriptor["conformsTo"]}
    assert "https://w3id.org/ro/crate/1.2" in profiles
    assert any("wfrun/provenance" in p for p in profiles)


def test_rocrate_emits_create_action_per_run(bundle: EvidenceBundle):
    """WRROC requires a CreateAction per run."""
    crate = to_rocrate(bundle)
    actions = [
        e for e in crate["@graph"]
        if e.get("@type") == "CreateAction"
    ]
    assert len(actions) == len(bundle.runs)


def test_rocrate_round_trip_preserves_bundle(bundle: EvidenceBundle):
    """from_rocrate(to_rocrate(b)) MUST equal b exactly."""
    crate = to_rocrate(bundle)
    restored = from_rocrate(crate)
    assert restored == bundle


def test_rocrate_round_trip_via_json_text(bundle: EvidenceBundle):
    """Round-trip survives JSON serialise/deserialise."""
    crate = to_rocrate(bundle)
    serialised = json.dumps(crate)
    restored = from_rocrate(json.loads(serialised))
    assert restored == bundle


def test_from_rocrate_rejects_missing_canonical_payload():
    crate = {"@context": "https://w3id.org/ro/crate/1.2/context", "@graph": [
        {"@id": "./", "@type": "Dataset"},  # no ai_orchestrator:bundle
    ]}
    with pytest.raises(ValueError, match="ai_orchestrator:bundle"):
        from_rocrate(crate)
