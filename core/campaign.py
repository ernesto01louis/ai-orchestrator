"""Campaign Pydantic models (Phase 1.1).

A Campaign is a parameterized multi-run experiment: a single template
plus a parameter grid produces N orchestrator runs. Lifecycle controls
(pause/resume/abort) are exposed at the campaign level. Persisted as a
JSON map keyed by campaign_id at ``memory/campaigns.json`` (locked I/O).

Domain-neutral by construction — works for any parameter sweep.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CampaignStatus = Literal[
    "queued", "running", "paused", "completed", "aborted", "failed"
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


class CampaignCreate(BaseModel):
    name: str
    description: str | None = None
    template: CampaignTemplate
    params: dict[str, list]
    max_runs: int | None = None
    parallelism: int = 1


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
