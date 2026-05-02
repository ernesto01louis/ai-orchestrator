"""RO-Crate 1.2 / WRROC emission and parsing for ``EvidenceBundle``.

We produce a JSON-LD document that is simultaneously:

1. A **valid RO-Crate 1.2** with the **Provenance Run Crate (WRROC)
   profile** declared in ``conformsTo`` — readable by every consumer
   that knows RO-Crate (eLife, WorkflowHub, Galaxy, ARC).
2. A **lossless round-trip carrier** for our Pydantic bundle — the
   full canonical bundle JSON is embedded under the
   ``ai_orchestrator:bundle`` property of the root Dataset entity, so
   ``from_rocrate(to_rocrate(b)) == b`` exactly.

The dual-view approach is intentional: a pure RDF decomposition would
lose information (the discriminated-union ``calculators[]`` dicts, the
``extra='allow'`` sampling params, etc.). Embedding the canonical JSON
keeps the bundle authoritative; the RO-Crate entities are a graph view
generated from it. Tools that index RO-Crates see an accurate picture;
tools that round-trip our bundles get bit-fidelity.

Spec references:

* RO-Crate 1.2: https://www.researchobject.org/ro-crate/specification/1.2/
* WRROC Provenance profile:
  https://www.researchobject.org/workflow-run-crate/profiles/provenance_run_crate/
"""
from __future__ import annotations

from typing import Any

from core.evidence import EvidenceBundle

# ── profile identifiers ───────────────────────────────

_RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.2/context"
_RO_CRATE_PROFILE = "https://w3id.org/ro/crate/1.2"
_WRROC_PROVENANCE_PROFILE = (
    "https://w3id.org/ro/wfrun/provenance/0.5"  # current WRROC profile URI
)
_AI_ORCHESTRATOR_BUNDLE_PROPERTY = "ai_orchestrator:bundle"
_AI_ORCHESTRATOR_NAMESPACE = {
    "ai_orchestrator": "https://ai-orchestrator.io/ns#",
}


def to_rocrate(bundle: EvidenceBundle) -> dict[str, Any]:
    """Serialise an ``EvidenceBundle`` as RO-Crate 1.2 / WRROC JSON-LD.

    The returned dict is the literal contents of
    ``ro-crate-metadata.json`` for the campaign's crate directory.
    Includes:

    * ``@context``: RO-Crate 1.2 + ai_orchestrator namespace
    * RO-Crate descriptor entity with ``conformsTo`` pointing at both
      RO-Crate 1.2 and the WRROC Provenance Run Crate profile
    * Root Dataset entity with the embedded canonical bundle
    * SoftwareApplication entities for each LLM target
    * CreateAction entities for each run (WRROC requirement)
    * ControlAction entity binding all runs to the campaign
    """
    descriptor = {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": [
            {"@id": _RO_CRATE_PROFILE},
            {"@id": _WRROC_PROVENANCE_PROFILE},
        ],
        "about": {"@id": "./"},
    }

    llm_entities = [_llm_target_entity(t, idx) for idx, t in enumerate(bundle.llm_targets)]
    run_entities = [_run_action_entity(r) for r in bundle.runs]
    artifact_entities = [_artifact_entity(a) for a in bundle.artifacts]

    control_action = {
        "@id": f"#campaign/{bundle.campaign_id}",
        "@type": "ControlAction",
        "name": bundle.campaign_name,
        "description": bundle.abstract,
        "object": [{"@id": e["@id"]} for e in run_entities],
        "instrument": [{"@id": e["@id"]} for e in llm_entities] or None,
        "startTime": bundle.created_at.isoformat(),
    }
    control_action = {k: v for k, v in control_action.items() if v is not None}

    root_dataset = {
        "@id": "./",
        "@type": "Dataset",
        "name": f"Evidence bundle: {bundle.campaign_name}",
        "description": bundle.abstract,
        "datePublished": bundle.created_at.isoformat(),
        "license": {"@id": "https://www.apache.org/licenses/LICENSE-2.0"},
        "identifier": bundle.bundle_id,
        "creator": {"@id": "#ai-orchestrator"},
        "mentions": [{"@id": control_action["@id"]}],
        "hasPart": [{"@id": e["@id"]} for e in artifact_entities],
        # Lossless round-trip carrier — keeps the Pydantic bundle authoritative.
        _AI_ORCHESTRATOR_BUNDLE_PROPERTY: bundle.model_dump(mode="json"),
    }

    builder_entity = {
        "@id": "#ai-orchestrator",
        "@type": "SoftwareApplication",
        "name": "ai-orchestrator",
        "url": "https://github.com/ernesto01louis/ai-orchestrator",
    }

    graph: list[dict[str, Any]] = [
        descriptor,
        root_dataset,
        builder_entity,
        control_action,
        *llm_entities,
        *run_entities,
        *artifact_entities,
    ]

    return {
        "@context": [_RO_CRATE_CONTEXT, _AI_ORCHESTRATOR_NAMESPACE],
        "@graph": graph,
    }


def from_rocrate(jsonld: dict[str, Any]) -> EvidenceBundle:
    """Parse an ``EvidenceBundle`` back from a RO-Crate JSON-LD document.

    Reads the embedded ``ai_orchestrator:bundle`` property on the root
    Dataset entity and validates it back to a Pydantic ``EvidenceBundle``.
    Raises ``ValueError`` if the property is missing — which means the
    crate wasn't produced by ``to_rocrate`` (or was hand-edited beyond
    the RO-Crate view).
    """
    graph = jsonld.get("@graph") or []
    for entity in graph:
        if entity.get("@id") == "./":
            payload = entity.get(_AI_ORCHESTRATOR_BUNDLE_PROPERTY)
            if payload is None:
                raise ValueError(
                    "RO-Crate root dataset is missing "
                    f"'{_AI_ORCHESTRATOR_BUNDLE_PROPERTY}' — was it produced "
                    "by evidence.rocrate.to_rocrate?"
                )
            return EvidenceBundle.model_validate(payload)
    raise ValueError("RO-Crate has no root dataset entity (./).")


# ── per-entity emitters ──────────────────────────────


def _llm_target_entity(target: Any, idx: int) -> dict[str, Any]:
    return {
        "@id": f"#llm/{target.role}/{idx}",
        "@type": "SoftwareApplication",
        "name": target.model_name,
        "applicationCategory": target.role,
        "operatingSystem": "Linux (Ollama)",
        "url": f"http://{target.host}",
        "softwareVersion": target.model_digest,
        "fileSize": str(target.model_size_bytes),
    }


def _run_action_entity(run: Any) -> dict[str, Any]:
    """Map a RunRecord to a WRROC-compliant CreateAction.

    ``object`` (inputs) carries the per-run parameters; ``result``
    (outputs) lists code-execution artifacts. We do not enumerate every
    LLM call as a separate Action here — they're embedded in the canonical
    bundle for round-trip and surfaced via the ``mentions`` link.
    """
    return {
        "@id": f"#run/{run.run_id}",
        "@type": "CreateAction",
        "name": f"Run {run.run_id}",
        "actionStatus": _action_status(run.status),
        "startTime": run.started_at.isoformat(),
        "endTime": run.finished_at.isoformat(),
        "object": [
            {
                "@id": f"#run/{run.run_id}/parameters",
                "@type": "PropertyValue",
                "name": "parameters",
                "value": str(run.parameters),
            }
        ],
    }


def _artifact_entity(art: Any) -> dict[str, Any]:
    return {
        "@id": art.path,
        "@type": "File",
        "encodingFormat": art.content_type,
        "contentSize": str(art.size_bytes),
        "sha256": art.sha256,
        "description": art.description or "",
        "ai_orchestrator:role": art.role,
    }


def _action_status(run_status: str) -> str:
    """Map our RunRecord.status to schema.org ActionStatusType."""
    return {
        "success": "https://schema.org/CompletedActionStatus",
        "fail": "https://schema.org/FailedActionStatus",
        "aborted": "https://schema.org/FailedActionStatus",
        "paused": "https://schema.org/PotentialActionStatus",
    }.get(run_status, "https://schema.org/CompletedActionStatus")
