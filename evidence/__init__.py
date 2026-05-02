"""Evidence-bundle plugin host (Phase 1.2).

Wires up the pluggy ``PluginManager`` for the
``ai_orchestrator_evidence`` namespace, registers builtin calculators,
and discovers third-party calculators via Python entry points.

Usage::

    from evidence import get_plugin_manager
    pm = get_plugin_manager()
    results = pm.hook.compute_evidence(campaign=c, runs=rs)  # list of lists
    flat = [r for sub in results for r in sub]
"""
from __future__ import annotations

import pluggy

from evidence.builtin import (
    code_fingerprint as _builtin_code_fingerprint,
    compute as _builtin_compute,
    hardware as _builtin_hardware,
    lineage as _builtin_lineage,
    stats as _builtin_stats,
)
from evidence.hookspecs import PLUGIN_NAMESPACE, EvidenceHookSpecs

_BUILTIN_PLUGINS = {
    "builtin_stats": _builtin_stats,
    "builtin_lineage": _builtin_lineage,
    "builtin_compute": _builtin_compute,
    "builtin_code_fingerprint": _builtin_code_fingerprint,
    "builtin_hardware": _builtin_hardware,
}


_pm: pluggy.PluginManager | None = None


def get_plugin_manager() -> pluggy.PluginManager:
    """Return the process-wide ``PluginManager`` for evidence calculators.

    Built lazily on first call: registers hookspecs, registers builtin
    calculators, then loads any third-party calculators registered via
    ``entry_points(group="ai_orchestrator_evidence")``.
    """
    global _pm
    if _pm is not None:
        return _pm

    pm = pluggy.PluginManager(PLUGIN_NAMESPACE)
    pm.add_hookspecs(EvidenceHookSpecs)

    for name, module in _BUILTIN_PLUGINS.items():
        pm.register(module, name=name)

    pm.load_setuptools_entrypoints(PLUGIN_NAMESPACE)

    _pm = pm
    return pm


def reset_plugin_manager() -> None:
    """Clear the cached PluginManager. Test-only — production code never calls this."""
    global _pm
    _pm = None
