"""manifest — per-run SHA256 manifests and campaign Merkle roots.

Phase 1.5 (SHA256 artifact manifests). Hook points into orchestration
come in Phases B–C; this package is pure compute/verify logic.
"""
from manifest.run_manifest import (
    VerifyResult,
    compute_run_manifest,
    verify_run_manifest,
    write_run_manifest,
)
from manifest.merkle import (
    compute_campaign_merkle,
    compute_merkle_root,
    verify_campaign_merkle,
    write_campaign_merkle,
)

__all__ = [
    "VerifyResult",
    "compute_run_manifest",
    "verify_run_manifest",
    "write_run_manifest",
    "compute_campaign_merkle",
    "compute_merkle_root",
    "verify_campaign_merkle",
    "write_campaign_merkle",
]
