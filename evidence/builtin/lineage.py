"""Lineage calculator.

Reports parent / child bundle relationships when a campaign has them.
Phase 1.2 only emits the "no known parent" record because the orchestrator
doesn't yet take a ``parent_bundle_id`` at campaign creation. Future
phases (1.6 Python client lib, 2.x federation) will populate this from
the ``Campaign.description`` or a dedicated field.

The calculator always emits a result (even when empty) so downstream
consumers can iterate ``bundle.calculators[].kind == "lineage"`` and
get a deterministic shape.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.evidence import CalculatorResult
from evidence.hookspecs import hookimpl

if TYPE_CHECKING:
    from core.campaign import Campaign
    from core.evidence import RunRecord


_CALCULATOR_ID = "ai_orchestrator.builtin.lineage:v1"
_OUTPUT_SCHEMA_VERSION = "1.0.0"


@hookimpl
def compute_evidence(
    campaign: "Campaign", runs: "list[RunRecord]"
) -> list[CalculatorResult]:
    started = time.monotonic()

    output = {
        "parent_bundle_ids": [],
        "child_bundle_ids": [],
        "external_refs": [],
    }

    duration_ms = int((time.monotonic() - started) * 1000)
    return [
        CalculatorResult(
            kind="lineage",
            calculator_id=_CALCULATOR_ID,
            schema_version=_OUTPUT_SCHEMA_VERSION,
            inputs={"campaign_id": campaign.id},
            output=output,
            duration_ms=duration_ms,
            deterministic=True,
        )
    ]
