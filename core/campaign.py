"""Campaign Pydantic models (Phase 1.1).

A Campaign is a parameterized multi-run experiment: a single template
plus a parameter grid produces N orchestrator runs. Lifecycle controls
(pause/resume/abort) are exposed at the campaign level. Persisted as a
JSON map keyed by campaign_id at ``memory/campaigns.json`` (locked I/O).

Domain-neutral by construction — works for any parameter sweep.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

CampaignStatus = Literal[
    "queued", "running", "paused", "completed", "aborted", "failed"
]


HITLMode = Literal[
    "full_auto", "gate_only", "checkpoint", "step_by_step", "co_pilot"
]


class CampaignTemplate(BaseModel):
    """OrchestrateRequest skeleton applied to every child run.

    String fields may contain ``{param}`` placeholders that are filled
    from the per-combo params dict at expansion time.
    """

    project_name: str
    prompt: str
    planner_model: str
    generator_models: list[str]
    judge_model: str
    deploy_target: str
    inspector_model: str | None = None
    optimizer_model: str | None = None
    troubleshooter_model: str | None = None
    max_iterations: int | None = None
    reference_files: list[str] | None = None
    # Phase 3.1 HITL intervention modes. Controls how aggressively the
    # orchestrator pauses for human input:
    #   full_auto    — today's behaviour (only Gates blocks pause)
    #   gate_only    — Gate denials route through HITL; otherwise auto
    #   checkpoint   — pauses at phase boundaries
    #                  (planner→generator→judge→optimizer)
    #   step_by_step — pauses after every LLM call (debug-only)
    #   co_pilot     — pauses BEFORE every LLM call to allow prompt
    #                  edits (debug-only)
    # ``None`` is accepted (older SDK clients send the field as null
    # when unset) and normalised to ``"full_auto"`` by
    # ``core.hitl.get_run_hitl_mode``.
    hitl_mode: HITLMode | None = None


class CampaignCreate(BaseModel):
    """Public POST body for creating a campaign.

    ``hypothesis`` is REQUIRED and load-bearing for the Phase 1.2
    evidence bundle: it satisfies REFORMS §1 pre-registration and
    is the field that lets the bundle honestly answer "what did this
    campaign conclude". Empty/whitespace-only values are rejected.
    """

    name: str
    description: str | None = None
    hypothesis: str
    template: CampaignTemplate
    params: dict[str, list]
    max_runs: int | None = None
    parallelism: int = 1

    @field_validator("hypothesis")
    @classmethod
    def _hypothesis_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "hypothesis required (REFORMS §1 pre-registration); "
                "give the question this campaign sets out to answer"
            )
        return v.strip()


class CampaignRun(BaseModel):
    run_id: str
    params: dict
    status: str = "queued"
    score: float | None = None


class Campaign(CampaignCreate):
    id: str
    status: CampaignStatus = "queued"
    runs: list[CampaignRun] = []
    created_at: str
    updated_at: str
    completed_at: str | None = None
