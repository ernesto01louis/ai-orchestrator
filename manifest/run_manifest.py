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

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from core.locks import locked_read_json, locked_write_json
from evidence.signing import sha256_file

# "skipped" is set by the orchestration hook (Phase B) when manifest writing is intentionally bypassed
ManifestStatus = Literal["ok", "corrupted", "missing", "skipped"]


@dataclass
class VerifyResult:
    """Outcome of a manifest verification.

    expected/actual are populated by verify_run_manifest (path -> sha256 maps)
    and left as None by verify_campaign_merkle (which uses mismatches only).
    """

    status: ManifestStatus
    mismatches: list[str] = field(default_factory=list)
    expected: dict[str, str] | None = None  # path -> sha256 (from manifest)
    actual: dict[str, str] | None = None  # path -> sha256 (from disk)

    @property
    def valid(self) -> bool:
        return self.status == "ok"


def _iter_run_files(run_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield (relative_posix_path, absolute_path) for every regular non-symlink file
    under run_dir, excluding only the root manifest.json itself."""
    for path in run_dir.rglob("*"):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        if rel == Path("manifest.json"):
            continue
        yield rel.as_posix(), path


def compute_run_manifest(run_dir: Path, run_id: str | None = None) -> dict[str, Any]:
    """Walk run_dir recursively, hash every regular file (excluding run_dir/manifest.json).

    Returns a JSON-serializable manifest dict. Files sorted by path for determinism.
    run_id defaults to run_dir.name if None.
    Symlinks (both file and dir) are skipped — the manifest only attests real in-tree files.
    """
    if run_id is None:
        run_id = run_dir.name

    files: list[dict[str, Any]] = sorted(
        (
            {
                "path": rel_posix,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for rel_posix, path in _iter_run_files(run_dir)
        ),
        key=lambda f: f["path"],
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

    Symlinks are skipped during the disk walk (consistent with compute_run_manifest).
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
    actual: dict[str, str] = {
        rel_posix: sha256_file(path)
        for rel_posix, path in _iter_run_files(run_dir)
    }

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
