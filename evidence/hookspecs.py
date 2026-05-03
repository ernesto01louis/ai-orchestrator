"""pluggy hook specification for evidence calculators.

Each calculator is a Python function decorated with ``@hookimpl`` that
takes a ``Campaign`` plus its list of ``RunRecord`` and returns a list
of ``CalculatorResult`` to be appended to the bundle's
``calculators[]`` list.

Calculators are discovered via the ``ai_orchestrator_evidence`` entry
point group — same pattern pytest uses for plugins. Consumer projects
register their own calculators in their own ``pyproject.toml`` without
touching this orchestrator (matches CLAUDE.md's "do NOT build domain
code into the orchestrator" rule).

Example registration in a downstream project::

    [project.entry-points."ai_orchestrator_evidence"]
    aero_drag = "aero_optim.evidence:drag_calculator"
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pluggy

if TYPE_CHECKING:
    from core.campaign import Campaign
    from core.evidence import CalculatorResult, RunRecord

PLUGIN_NAMESPACE = "ai_orchestrator_evidence"

hookspec = pluggy.HookspecMarker(PLUGIN_NAMESPACE)
hookimpl = pluggy.HookimplMarker(PLUGIN_NAMESPACE)


class EvidenceHookSpecs:
    """Hookspecs for the evidence-bundle plugin namespace."""

    @hookspec
    def compute_evidence(
        self, campaign: "Campaign", runs: "list[RunRecord]"
    ) -> "list[CalculatorResult]":
        """Compute zero or more CalculatorResult entries for a campaign.

        Implementations MUST be deterministic given identical inputs
        unless their CalculatorResult.deterministic field is set False.
        Implementations MUST NOT mutate ``campaign`` or ``runs``.
        Implementations MAY return an empty list when the calculator
        is not applicable (e.g. param_importance with a single param).
        """
        ...
