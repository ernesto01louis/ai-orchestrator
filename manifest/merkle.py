"""Campaign-level Merkle tree over per-run manifest.json hashes.

Schema (campaign_dir/merkle.json):
{
  "version": 1,
  "campaign_id": "<uuid>",
  "created_at": "<iso8601 utc>",
  "runs": [
    {"run_id": "<uuid>", "project_name": "<name>", "manifest_sha256": "<hex>"}
  ],
  "merkle_root": "<hex>"
}
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from core.locks import locked_read_json, locked_write_json
from evidence.signing import sha256_file
from manifest.run_manifest import VerifyResult


def compute_merkle_root(leaves: list[bytes]) -> str:
    """Pure binary Merkle tree (pairwise sha256), hex output.

    - Empty: sha256(b"") hex (sentinel for empty tree).
    - 1 leaf: sha256(leaf) hex.
    - 2 leaves: sha256(L0 || L1) hex.
    - Odd count at any level: duplicate the last node.
    Iterates level-by-level until a single root node remains.
    """
    if not leaves:
        return hashlib.sha256(b"").hexdigest()

    level: list[bytes] = [hashlib.sha256(leaf).digest() for leaf in leaves]

    while len(level) > 1:
        next_level: list[bytes] = []
        # Pad to even length by duplicating the last node
        if len(level) % 2 == 1:
            level.append(level[-1])
        for i in range(0, len(level), 2):
            next_level.append(hashlib.sha256(level[i] + level[i + 1]).digest())
        level = next_level

    return level[0].hex()


def compute_campaign_merkle(
    campaign_dir: Path,
    run_dirs: list[tuple[str, str, Path]],  # (run_id, project_name, run_dir)
    campaign_id: str | None = None,
) -> dict:
    """Compute a campaign Merkle dict from the given run directories.

    For each run_dir, hashes its manifest.json. If missing, uses a deterministic
    sentinel: sha256(b"MISSING_MANIFEST:" + run_id.encode()).hexdigest().

    Runs are sorted by run_id (lexicographic UUID order) before building leaves.
    campaign_id defaults to campaign_dir.name if None.
    """
    if campaign_id is None:
        campaign_id = campaign_dir.name

    # Sort by run_id for determinism
    sorted_runs = sorted(run_dirs, key=lambda t: t[0])

    run_entries: list[dict] = []
    leaves: list[bytes] = []

    for run_id, project_name, run_dir in sorted_runs:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            manifest_sha256 = sha256_file(manifest_path)
        else:
            manifest_sha256 = hashlib.sha256(
                b"MISSING_MANIFEST:" + run_id.encode()
            ).hexdigest()

        run_entries.append(
            {
                "run_id": run_id,
                "project_name": project_name,
                "manifest_sha256": manifest_sha256,
            }
        )
        leaves.append(bytes.fromhex(manifest_sha256))

    merkle_root = compute_merkle_root(leaves)

    return {
        "version": 1,
        "campaign_id": campaign_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": run_entries,
        "merkle_root": merkle_root,
    }


def write_campaign_merkle(
    campaign_dir: Path,
    run_dirs: list[tuple[str, str, Path]],
    campaign_id: str | None = None,
) -> Path:
    """Compute campaign merkle and write to campaign_dir/merkle.json via locked_write_json.

    Returns the path to the written file.
    """
    data = compute_campaign_merkle(campaign_dir, run_dirs, campaign_id=campaign_id)
    dest = campaign_dir / "merkle.json"
    locked_write_json(dest, data)
    return dest


def verify_campaign_merkle(campaign_dir: Path, projects_root: Path) -> VerifyResult:
    """Re-read merkle.json, recompute manifest_sha256 for each listed run, rebuild tree.

    Run directories are resolved as:
        projects_root / <project_name> / runs / <run_id>

    Status:
      - "missing"   if merkle.json doesn't exist
      - "corrupted" if recomputed root != stored root, or any run's manifest hash differs
      - "ok"        if everything matches

    Mismatches: list of run_ids whose manifest_sha256 changed, plus 'merkle_root'
    if only the root differs.
    """
    merkle_path = campaign_dir / "merkle.json"
    if not merkle_path.exists():
        return VerifyResult(status="missing")

    data = locked_read_json(merkle_path, default=None)
    if data is None:
        return VerifyResult(status="corrupted", mismatches=["merkle.json (unparseable)"])

    stored_root: str = data.get("merkle_root", "")
    runs: list[dict] = data.get("runs", [])

    mismatches: list[str] = []
    leaves: list[bytes] = []

    for entry in runs:
        run_id: str = entry["run_id"]
        project_name: str = entry["project_name"]
        stored_sha256: str = entry["manifest_sha256"]

        run_dir = projects_root / project_name / "runs" / run_id
        manifest_path = run_dir / "manifest.json"

        if manifest_path.exists():
            current_sha256 = sha256_file(manifest_path)
        else:
            current_sha256 = hashlib.sha256(
                b"MISSING_MANIFEST:" + run_id.encode()
            ).hexdigest()

        if current_sha256 != stored_sha256:
            mismatches.append(run_id)

        leaves.append(bytes.fromhex(current_sha256))

    recomputed_root = compute_merkle_root(leaves)

    if recomputed_root != stored_root and not mismatches:
        # Root differs but no individual run changed — structural inconsistency
        mismatches.append("merkle_root")

    if mismatches:
        return VerifyResult(status="corrupted", mismatches=sorted(mismatches))

    return VerifyResult(status="ok")
