"""Citation-grade evidence bundle schemas (Phase 1.2).

Defines a Pydantic v2 data model for an ``EvidenceBundle`` that:

* proves a campaign happened (cryptographically tamper-evident manifest)
* states what it concluded (typed calculator outputs)
* carries enough provenance for a third-party reader to manually
  reproduce the experiment from the bundle alone

The schema is wire-compatible with industry-standard supply-chain and
research-packaging specs:

* `DSSE`_ envelope wraps signatures (algo-agnostic).
* `in-toto Statement v1`_ + `SLSA Provenance v1.0`_ predicate carry
  the attestation payload.
* The whole bundle round-trips to/from `RO-Crate 1.2`_ JSON-LD via
  the `Provenance Run Crate (WRROC)`_ profile (mapper lives in a
  separate module so this file stays free of JSON-LD concerns).
* Reproducibility-checklist responses follow `REFORMS`_ (Kapoor et al.,
  *Sci Adv* 2024) and the `NeurIPS Paper Checklist`_.

.. _DSSE: https://github.com/secure-systems-lab/dsse
.. _in-toto Statement v1: https://github.com/in-toto/attestation
.. _SLSA Provenance v1.0: https://slsa.dev/spec/v1.0/provenance
.. _RO-Crate 1.2: https://www.researchobject.org/ro-crate/specification/1.2/
.. _Provenance Run Crate (WRROC): https://www.researchobject.org/workflow-run-crate/profiles/provenance_run_crate/
.. _REFORMS: https://reforms.cs.princeton.edu/
.. _NeurIPS Paper Checklist: https://neurips.cc/public/guides/PaperChecklist
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field

# ── DSSE envelope (Dead Simple Signing Envelope) ──────

class DsseSignature(BaseModel):
    """One signature inside a DSSE envelope.

    `cert` carries an X.509 cert when signing via Sigstore; for minisign
    it stays None and `keyid` is the Ed25519 fingerprint.
    """
    keyid: str
    sig: str  # base64
    cert: str | None = None


class DsseEnvelope(BaseModel):
    """DSSE envelope: a payload + payloadType + N signatures.

    The DSSE ``PAE`` (pre-authentication encoding) is what's signed —
    not the raw payload — so signature equality is independent of
    payload base64 encoding. See secure-systems-lab/dsse for spec.
    """
    payload: str  # base64(canonical_json(InTotoStatement))
    payloadType: Literal["application/vnd.in-toto+json"] = "application/vnd.in-toto+json"
    signatures: list[DsseSignature]


# ── in-toto Statement v1 + SLSA Provenance v1.0 ──────

class Subject(BaseModel):
    """One artifact referenced by an in-toto Statement.

    `digest` is keyed by hash algorithm name (per in-toto spec); we
    only emit sha256 today, but the dict shape leaves room for blake3.
    """
    name: str
    digest: dict[Literal["sha256"], str]


class SlsaBuilder(BaseModel):
    """Identifies the builder (here: the orchestrator) that ran the build."""
    id: AnyUrl
    version: dict[str, str]  # {"ai-orchestrator": "0.2.0", "ollama": "0.5.x", ...}


class SlsaBuildMetadata(BaseModel):
    invocationId: str  # our run_id / campaign_id
    startedOn: datetime
    finishedOn: datetime


class SlsaBuildDefinition(BaseModel):
    """What was built — fully captures the inputs.

    `externalParameters` MUST include everything a reader needs to
    re-run the build identically (campaign config, prompts, params,
    sampling). `internalParameters` are orchestrator-internal details
    (URL routing, gates state) that affect the build but aren't part
    of the user-facing definition.
    """
    buildType: AnyUrl = Field(default="https://ai-orchestrator.io/campaign/v1")
    externalParameters: dict
    internalParameters: dict
    resolvedDependencies: list[Subject]


class SlsaRunDetails(BaseModel):
    builder: SlsaBuilder
    metadata: SlsaBuildMetadata
    byproducts: list[Subject] = []  # logs, intermediate files


class SlsaProvenance(BaseModel):
    """SLSA Provenance v1.0 predicate body."""
    buildDefinition: SlsaBuildDefinition
    runDetails: SlsaRunDetails


class InTotoStatement(BaseModel):
    """in-toto Statement v1 — the thing we sign.

    `_type` is reserved at the protocol level; we serialize via the
    alias to preserve underscore semantics in JSON.
    """
    type_: Literal["https://in-toto.io/Statement/v1"] = Field(
        default="https://in-toto.io/Statement/v1", alias="_type"
    )
    subject: list[Subject]
    predicateType: AnyUrl = Field(default="https://slsa.dev/provenance/v1")
    predicate: SlsaProvenance

    model_config = ConfigDict(populate_by_name=True)


# ── reproducibility-core fingerprints ─────────────────

class GpuInfo(BaseModel):
    model: str
    vram_gb: float
    driver: str


class HardwareFingerprint(BaseModel):
    """NeurIPS Q8 / REFORMS §2 — what hardware did this run on."""
    cpu_model: str
    cpu_count: int
    ram_gb: float
    gpus: list[GpuInfo] = []
    os: str
    kernel: str
    cuda: str | None = None
    hostname: str | None = None


class CodeFingerprint(BaseModel):
    """NeurIPS Q5 / REFORMS §2 — what code state produced this run.

    `requirements_lock` carries the full lock contents (pip freeze /
    uv.lock) so the bundle is self-contained; `requirements_lock_sha256`
    fingerprints it for signing.
    """
    git_remote: str  # may be ssh:// or https://, hence plain str
    git_sha: str
    git_dirty: bool
    requirements_lock: str
    requirements_lock_sha256: str


class LlmTarget(BaseModel):
    """One LLM endpoint used in the campaign.

    `model_digest` is the Ollama-style sha256 the model can be pinned
    to via ``FROM <name>@<digest>`` in a Modelfile — the *only* way to
    re-pull bit-identical model weights.
    """
    role: Literal[
        "planner", "judge", "generator", "optimizer",
        "troubleshooter", "tool_dispatch",
    ]
    host: str  # "192.168.2.13:11434"
    model_name: str
    model_digest: str  # sha256-… from `ollama show`
    model_size_bytes: int


class SamplingParams(BaseModel):
    """Sampling settings for a single LLM call.

    NOTE on determinism: per vLLM and Ollama documentation, even with
    ``seed`` set and ``temperature=0`` results can differ across
    hardware. The bundle's caveats section calls this out; do not
    interpret seed alone as bit-reproducibility.

    `extra="allow"` lets backend-specific knobs (e.g. ``mirostat``)
    survive without us versioning them in the schema.
    """
    model_config = ConfigDict(extra="allow")
    temperature: float
    top_p: float | None = None
    top_k: int | None = None
    seed: int | None = None
    num_ctx: int | None = None


class LlmCall(BaseModel):
    """One LLM /api/chat or /api/generate invocation."""
    call_id: str
    role: str
    target: LlmTarget
    rendered_messages: list[dict]  # exact JSON sent to the LLM
    sampling: SamplingParams
    response_text: str
    response_tokens: int
    latency_ms: int
    started_at: datetime


class CodeExecution(BaseModel):
    """One sandbox/SSH execution of generated code."""
    language: str
    code_sha256: str
    stdout: str
    stderr: str
    return_code: int
    duration_ms: int


class RunRecord(BaseModel):
    """One point in the campaign's parameter sweep — a single run.

    Maps 1:1 to the existing ``Campaign.runs[i]`` plus the per-run
    artifacts under ``projects/{name}/runs/{run_id}/`` that 1.1 already
    captures. Bundle emission is pure repackaging, not new capture.
    """
    run_id: str
    parameters: dict
    llm_calls: list[LlmCall] = []
    code_executions: list[CodeExecution] = []
    metrics: dict[str, float] = {}
    status: Literal["success", "fail", "aborted", "paused"]
    started_at: datetime
    finished_at: datetime


# ── pluggable evidence calculators ────────────────────

class CalculatorResult(BaseModel):
    """Output of one evidence-calculator plugin.

    Plugins are registered via the ``ai_orchestrator_evidence`` entry
    point group (pluggy hooks). Each result carries its OWN
    ``schema_version`` so calculators can evolve independently of the
    bundle schema. ``inputs`` records what the calculator consumed so
    a reader can replay it; ``deterministic`` flags whether replay
    will yield the same output.
    """
    kind: str  # "statistical_summary", "compute_resources", ...
    calculator_id: str  # "ai_orchestrator.builtin.stats:v1"
    schema_version: str  # semver of THIS calculator's output
    inputs: dict
    output: dict
    duration_ms: int
    deterministic: bool


# ── per-file metadata ─────────────────────────────────

class Artifact(BaseModel):
    """One file referenced by the bundle.

    Mirrors the RO-Crate ``hasPart`` entity. ``path`` is relative to
    the crate root; ``sha256`` is what gets signed (via the manifest
    Subject list) so readers can independently verify integrity.
    """
    path: str
    sha256: str
    content_type: str  # MIME
    size_bytes: int
    description: str | None = None
    role: Literal[
        "log", "code", "config", "result",
        "figure", "checklist", "model_card", "datasheet", "other",
    ]


# ── the root ──────────────────────────────────────────

class EvidenceBundle(BaseModel):
    """Citation-grade evidence bundle for a single campaign.

    Round-trip invariant: ``EvidenceBundle.model_validate(b.model_dump()) == b``.

    Round-trip via RO-Crate (``evidence.rocrate.to_rocrate`` /
    ``from_rocrate``) MUST also preserve the bundle's logical content;
    test_evidence_rocrate.py locks this in.

    The required ``hypothesis`` field is the REFORMS §1 pre-registration
    statement. The campaign-creation API rejects campaigns without one
    so the bundle can honestly answer "what did this conclude."
    """
    schema_version: Literal["1.0.0"] = "1.0.0"
    bundle_id: str  # ULID
    campaign_id: str
    campaign_name: str
    created_at: datetime
    abstract: str  # 1-paragraph human summary
    hypothesis: str  # REFORMS §1 pre-registration — REQUIRED

    # Reproducibility core (NeurIPS Q4-Q8, REFORMS §2 §5 §7).
    code: CodeFingerprint
    hardware: HardwareFingerprint
    llm_targets: list[LlmTarget] = []
    runs: list[RunRecord] = []

    # Checklists — Markdown blobs, half auto-filled, half user-fillable.
    model_cards: dict[str, str] = {}  # keyed by LlmTarget.model_name
    datasheets: dict[str, str] = {}  # keyed by data-input identifier
    reforms_responses: dict[str, str] = {}  # 32 keys (REFORMS items)
    neurips_responses: dict[str, str] = {}  # NeurIPS Q1..Q15

    # Plugin results.
    calculators: list[CalculatorResult] = []

    # Provenance / supply-chain attestation.
    attestations: list[DsseEnvelope] = []
    lineage: list[str] = []  # parent bundle_ids

    # Per-file index (mirrors RO-Crate hasPart).
    artifacts: list[Artifact] = []
