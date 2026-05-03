"""Tests for the EvidenceBundle Pydantic schema (Phase 1.2).

Locks in:
* round-trip equality via model_dump / model_validate
* hypothesis required at construction
* sub-types for in-toto / SLSA / DSSE accept the expected shapes
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core.evidence import (
    Artifact,
    CalculatorResult,
    CodeFingerprint,
    DsseEnvelope,
    DsseSignature,
    EvidenceBundle,
    HardwareFingerprint,
    InTotoStatement,
    LlmCall,
    LlmTarget,
    RunRecord,
    SamplingParams,
    SlsaBuildDefinition,
    SlsaBuilder,
    SlsaBuildMetadata,
    SlsaProvenance,
    SlsaRunDetails,
    Subject,
)


def _minimal_bundle(**overrides) -> EvidenceBundle:
    fields = {
        "bundle_id": "B1",
        "campaign_id": "C1",
        "campaign_name": "smoke",
        "created_at": datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
        "abstract": "tiny abstract",
        "hypothesis": "tests preserve their typed-field shape",
        "code": CodeFingerprint(
            git_remote="https://github.com/x/y", git_sha="abc",
            git_dirty=False, requirements_lock="",
            requirements_lock_sha256="0" * 64,
        ),
        "hardware": HardwareFingerprint(
            cpu_model="x", cpu_count=1, ram_gb=1.0, os="linux", kernel="6.x",
        ),
    }
    fields.update(overrides)
    return EvidenceBundle(**fields)


def test_minimal_bundle_round_trips():
    b = _minimal_bundle()
    assert EvidenceBundle.model_validate(b.model_dump()) == b


def test_full_bundle_round_trips():
    target = LlmTarget(
        role="judge", host="127.0.0.1:11434", model_name="m",
        model_digest="sha256-abc", model_size_bytes=1,
    )
    call = LlmCall(
        call_id="c1", role="judge", target=target,
        rendered_messages=[{"role": "user", "content": "go"}],
        sampling=SamplingParams(temperature=0.7, seed=42),
        response_text="ok", response_tokens=12, latency_ms=300,
        started_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    run = RunRecord(
        run_id="r1", parameters={"seed": 1}, llm_calls=[call],
        metrics={"score": 7.5}, status="success",
        started_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 3, 12, 1, 0, tzinfo=timezone.utc),
    )
    artifact = Artifact(
        path="evidence.json", sha256="0" * 64,
        content_type="application/json", size_bytes=1, role="config",
    )
    calculator = CalculatorResult(
        kind="statistical_summary",
        calculator_id="ai_orchestrator.builtin.stats:v1",
        schema_version="1.0.0",
        inputs={"metric": "score"},
        output={"mean": 7.5}, duration_ms=1, deterministic=True,
    )
    statement = InTotoStatement(
        subject=[Subject(name="evidence.json", digest={"sha256": "0" * 64})],
        predicate=SlsaProvenance(
            buildDefinition=SlsaBuildDefinition(
                externalParameters={}, internalParameters={},
                resolvedDependencies=[],
            ),
            runDetails=SlsaRunDetails(
                builder=SlsaBuilder(
                    id="https://ai-orchestrator.io/builder/v0.1",
                    version={"ai-orchestrator": "0.1.2"},
                ),
                metadata=SlsaBuildMetadata(
                    invocationId="C1",
                    startedOn=datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
                    finishedOn=datetime(2026, 5, 3, 12, 1, 0, tzinfo=timezone.utc),
                ),
            ),
        ),
    )
    envelope = DsseEnvelope(
        payload="cGF5bG9hZA==",
        signatures=[DsseSignature(keyid="abc", sig="c2ln")],
    )

    bundle = _minimal_bundle(
        llm_targets=[target], runs=[run], artifacts=[artifact],
        calculators=[calculator], attestations=[envelope],
    )
    assert EvidenceBundle.model_validate(bundle.model_dump()) == bundle
    assert statement.predicateType == "https://slsa.dev/provenance/v1"


def test_hypothesis_required():
    """The bundle's `hypothesis` field is non-optional."""
    with pytest.raises(ValidationError, match="hypothesis"):
        EvidenceBundle(
            bundle_id="B1", campaign_id="C1", campaign_name="x",
            created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            abstract="x",
            code=CodeFingerprint(
                git_remote="x", git_sha="x", git_dirty=False,
                requirements_lock="", requirements_lock_sha256="0" * 64,
            ),
            hardware=HardwareFingerprint(
                cpu_model="x", cpu_count=1, ram_gb=1.0,
                os="linux", kernel="6.x",
            ),
        )


def test_intoto_statement_emits_alias_for_type_field():
    """The `_type` JSON property must round-trip through `type_` in Python."""
    s = InTotoStatement(
        subject=[Subject(name="x", digest={"sha256": "0" * 64})],
        predicate=SlsaProvenance(
            buildDefinition=SlsaBuildDefinition(
                externalParameters={}, internalParameters={},
                resolvedDependencies=[],
            ),
            runDetails=SlsaRunDetails(
                builder=SlsaBuilder(id="x", version={}),
                metadata=SlsaBuildMetadata(
                    invocationId="x",
                    startedOn=datetime(2026, 5, 3, tzinfo=timezone.utc),
                    finishedOn=datetime(2026, 5, 3, tzinfo=timezone.utc),
                ),
            ),
        ),
    )
    dumped = s.model_dump(by_alias=True)
    assert "_type" in dumped
    assert dumped["_type"] == "https://in-toto.io/Statement/v1"
