"""Built-in evidence calculators shipped with the orchestrator.

Five calculators register against the ``ai_orchestrator_evidence``
hook namespace: ``stats``, ``lineage``, ``compute``,
``code_fingerprint``, ``hardware``.

External projects ship their own calculators via Python entry points;
the builtin set is registered programmatically in ``evidence.__init__``
to avoid round-tripping through entry-point metadata for first-party
code.
"""
