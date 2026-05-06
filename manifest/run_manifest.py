"""Per-run SHA256 manifest: compute, write, and verify.

Schema (run_dir/manifest.json):
{
  "version": 1,
  "run_id": "<uuid>",
  "created_at": "<iso8601 utc>",
  "files": [
    {"path": "<relative POSIX path>", "sha256": "<hex>", "size_bytes": <int>}
  ]
}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.locks import locked_read_json, locked_write_json
from evidence.signing import sha256_file

ManifestStatus = Literal["ok", "corrupted", "missing", "skipped"]


@dataclass
class VerifyResult:
    status: ManifestStatus
    mismatches: list[str] = field(default_factory=list)
    expected: dict[str, str] | None = None  # path -> sha256 (from manifest)
    actual: dict[str, str] | None = None  # path -> sha256 (from disk)

    @property
    def valid(self) -> bool:
        return self.status == "ok"


def compute_run_manifest(run_dir: Path, run_id: str | None = None) -> dict:
    """Walk run_dir recursively, hash every regular file (excluding run_dir/manifest.json).

    Returns a JSON-serializable manifest dict. Files sorted by path for determinism.
    run_id defaults to run_dir.name if None.
    """
    if run_id is None:
        run_id = run_dir.name

    self_manifest = Path("manifest.json")
    files: list[dict] = []

    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        # Exclude only the root-level manifest.json itself
        if rel == self_manifest:
            continue
        files.append(
            {
                "path": str(rel.as_posix()),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )

    return {
        "version": 1,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }


def write_run_manifest(run_dir: Path, run_id: str | None = None) -> Path:
    """Compute manifest and write to run_dir/manifest.json via locked_write_json.

    Returns the path to the written file.
    """
    manifest = compute_run_manifest(run_dir, run_id=run_id)
    dest = run_dir / "manifest.json"
    locked_write_json(dest, manifest)
    return dest


def verify_run_manifest(run_dir: Path) -> VerifyResult:
    """Read run_dir/manifest.json, recompute hashes, and compare.

    Status:
      - "missing"   if manifest.json doesn't exist
      - "corrupted" if any sha256 doesn't match, or files are missing/extra on disk
      - "ok"        if everything matches
    """
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return VerifyResult(status="missing")

    data = locked_read_json(manifest_path, default=None)
    if data is None:
        return VerifyResult(status="corrupted", mismatches=["manifest.json (unparseable)"])

    # Build expected map: relative-posix-path -> sha256
    expected: dict[str, str] = {entry["path"]: entry["sha256"] for entry in data.get("files", [])}

    # Build actual map by re-hashing every file on disk (excluding manifest.json itself)
    self_manifest = Path("manifest.json")
    actual: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        if rel == self_manifest:
            continue
        actual[str(rel.as_posix())] = sha256_file(path)

    mismatches: list[str] = []

    # Files in manifest but missing on disk or with wrong hash
    for rel_path, exp_hash in expected.items():
        if rel_path not in actual:
            mismatches.append(rel_path)  # missing on disk
        elif actual[rel_path] != exp_hash:
            mismatches.append(rel_path)  # hash mismatch

    # Files on disk but not recorded in manifest
    for rel_path in actual:
        if rel_path not in expected:
            mismatches.append(rel_path)

    if mismatches:
        return VerifyResult(
            status="corrupted",
            mismatches=sorted(mismatches),
            expected=expected,
            actual=actual,
        )

    return VerifyResult(status="ok", expected=expected, actual=actual)
