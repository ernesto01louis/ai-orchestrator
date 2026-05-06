"""Unit tests for the manifest/ package (Phase 1.5 — Phase A).

Covers:
  - per-run SHA256 manifest: compute, write, verify
  - pure Merkle tree: edge cases, determinism, tamper detection
  - campaign merkle: two-run, missing manifest sentinel, write+verify roundtrip,
    corrupted-run detection
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from manifest import (
    compute_campaign_merkle,
    compute_merkle_root,
    compute_run_manifest,
    verify_campaign_merkle,
    verify_run_manifest,
    write_campaign_merkle,
    write_run_manifest,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Run manifest: compute ─────────────────────────────────────────────────────


def test_compute_run_manifest_basic(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-abc"
    run_dir.mkdir()
    _write(run_dir / "a.txt", b"hello")
    _write(run_dir / "b.txt", b"world")
    _write(run_dir / "src" / "c.py", b"x = 1")

    manifest = compute_run_manifest(run_dir, run_id="run-abc")

    assert manifest["version"] == 1
    assert manifest["run_id"] == "run-abc"
    assert "created_at" in manifest

    files = manifest["files"]
    # Should have exactly 3 entries, sorted by path
    assert len(files) == 3
    paths = [f["path"] for f in files]
    assert paths == sorted(paths)

    # Verify sha256 and size for each file
    by_path = {f["path"]: f for f in files}
    assert by_path["a.txt"]["sha256"] == _sha256(b"hello")
    assert by_path["a.txt"]["size_bytes"] == 5
    assert by_path["b.txt"]["sha256"] == _sha256(b"world")
    assert by_path["src/c.py"]["sha256"] == _sha256(b"x = 1")


def test_compute_run_manifest_run_id_defaults_to_dir_name(tmp_path: Path) -> None:
    run_dir = tmp_path / "my-run-id"
    run_dir.mkdir()
    _write(run_dir / "f.txt", b"data")

    manifest = compute_run_manifest(run_dir)
    assert manifest["run_id"] == "my-run-id"


def test_compute_run_manifest_excludes_self(tmp_path: Path) -> None:
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _write(run_dir / "data.txt", b"payload")
    # Pre-existing root manifest.json should not appear in files
    _write(run_dir / "manifest.json", b'{"version": 0}')

    manifest = compute_run_manifest(run_dir)
    paths = [f["path"] for f in manifest["files"]]
    assert "manifest.json" not in paths
    assert "data.txt" in paths


def test_compute_run_manifest_includes_nested_manifest_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "run2"
    run_dir.mkdir()
    _write(run_dir / "src" / "manifest.json", b"nested")

    manifest = compute_run_manifest(run_dir)
    paths = [f["path"] for f in manifest["files"]]
    # nested manifest.json should be tracked; only run_dir/manifest.json is excluded
    assert "src/manifest.json" in paths


# ── Run manifest: write + verify ──────────────────────────────────────────────


def test_write_then_verify_ok(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-ok"
    run_dir.mkdir()
    _write(run_dir / "out.txt", b"results")
    _write(run_dir / "config.yaml", b"lr: 0.001")

    manifest_path = write_run_manifest(run_dir)
    assert manifest_path == run_dir / "manifest.json"
    assert manifest_path.exists()

    result = verify_run_manifest(run_dir)
    assert result.valid
    assert result.status == "ok"
    assert result.mismatches == []


def test_verify_missing_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "empty-run"
    run_dir.mkdir()

    result = verify_run_manifest(run_dir)
    assert result.status == "missing"
    assert not result.valid


def test_verify_corrupted_after_byte_flip(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-flip"
    run_dir.mkdir()
    target = run_dir / "weights.bin"
    _write(target, b"\x00\x01\x02\x03\x04")
    _write(run_dir / "meta.txt", b"info")

    write_run_manifest(run_dir)

    # Flip a byte in the tracked file
    data = bytearray(target.read_bytes())
    data[0] ^= 0xFF
    target.write_bytes(bytes(data))

    result = verify_run_manifest(run_dir)
    assert result.status == "corrupted"
    assert "weights.bin" in result.mismatches


def test_verify_extra_file_on_disk(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-extra"
    run_dir.mkdir()
    _write(run_dir / "original.txt", b"original")

    write_run_manifest(run_dir)

    # Add a new file after manifest was written
    _write(run_dir / "sneaky.txt", b"not in manifest")

    result = verify_run_manifest(run_dir)
    assert result.status == "corrupted"
    assert "sneaky.txt" in result.mismatches


def test_verify_missing_file_on_disk(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-missing-file"
    run_dir.mkdir()
    tracked = run_dir / "important.txt"
    _write(tracked, b"important data")

    write_run_manifest(run_dir)

    # Delete a tracked file
    tracked.unlink()

    result = verify_run_manifest(run_dir)
    assert result.status == "corrupted"
    assert "important.txt" in result.mismatches


# ── Merkle tree: pure unit tests ──────────────────────────────────────────────


def test_merkle_empty_returns_empty_sha256() -> None:
    expected = _sha256(b"")
    assert compute_merkle_root([]) == expected


def test_merkle_single_leaf() -> None:
    leaf = b"x"
    expected = _sha256(leaf)
    assert compute_merkle_root([leaf]) == expected


def test_merkle_pair() -> None:
    L0 = b"leaf0"
    L1 = b"leaf1"
    h0 = hashlib.sha256(L0).digest()
    h1 = hashlib.sha256(L1).digest()
    expected = hashlib.sha256(h0 + h1).hexdigest()
    assert compute_merkle_root([L0, L1]) == expected


def test_merkle_odd_count_duplicates_last() -> None:
    L0 = b"aaa"
    L1 = b"bbb"
    L2 = b"ccc"
    # Level 1: sha256(L0), sha256(L1), sha256(L2)
    # Pad to even: sha256(L0), sha256(L1), sha256(L2), sha256(L2)
    # Level 2: sha256(sha256(L0)||sha256(L1)), sha256(sha256(L2)||sha256(L2))
    # Root:    sha256(level2[0] || level2[1])
    h0 = hashlib.sha256(L0).digest()
    h1 = hashlib.sha256(L1).digest()
    h2 = hashlib.sha256(L2).digest()
    pair01 = hashlib.sha256(h0 + h1).digest()
    pair22 = hashlib.sha256(h2 + h2).digest()
    expected = hashlib.sha256(pair01 + pair22).hexdigest()
    assert compute_merkle_root([L0, L1, L2]) == expected


def test_merkle_deterministic() -> None:
    leaves = [b"a", b"b", b"c", b"d"]
    root1 = compute_merkle_root(leaves)
    root2 = compute_merkle_root(leaves)
    assert root1 == root2


def test_merkle_tamper_detection() -> None:
    leaves = [b"leaf0", b"leaf1", b"leaf2", b"leaf3"]
    original_root = compute_merkle_root(leaves)

    for i in range(len(leaves)):
        tampered = list(leaves)
        tampered[i] = b"tampered"
        assert compute_merkle_root(tampered) != original_root, f"flip at index {i} not detected"


# ── Campaign merkle ───────────────────────────────────────────────────────────


def _make_run(projects_root: Path, project_name: str, run_id: str, content: bytes) -> Path:
    """Create a run dir with one file and a written manifest. Returns run_dir."""
    run_dir = projects_root / project_name / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write(run_dir / "output.txt", content)
    write_run_manifest(run_dir, run_id=run_id)
    return run_dir


def test_compute_campaign_merkle_two_runs(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    campaign_dir = tmp_path / "campaign-1"
    campaign_dir.mkdir()

    run_dir_a = _make_run(projects_root, "proj-x", "run-aaaa", b"result_a")
    run_dir_b = _make_run(projects_root, "proj-x", "run-bbbb", b"result_b")

    run_dirs = [
        ("run-aaaa", "proj-x", run_dir_a),
        ("run-bbbb", "proj-x", run_dir_b),
    ]

    data = compute_campaign_merkle(campaign_dir, run_dirs, campaign_id="campaign-1")

    assert data["version"] == 1
    assert data["campaign_id"] == "campaign-1"
    assert "created_at" in data
    assert "merkle_root" in data
    assert len(data["merkle_root"]) == 64  # sha256 hex

    # Runs sorted by run_id
    run_ids = [r["run_id"] for r in data["runs"]]
    assert run_ids == sorted(run_ids)

    # Deterministic: recompute and compare root
    data2 = compute_campaign_merkle(campaign_dir, run_dirs, campaign_id="campaign-1")
    assert data["merkle_root"] == data2["merkle_root"]


def test_campaign_merkle_missing_manifest_uses_sentinel(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    campaign_dir = tmp_path / "campaign-sentinel"
    campaign_dir.mkdir()

    run_id = "run-missing"
    # Run dir exists but has no manifest.json
    run_dir = projects_root / "proj-y" / "runs" / run_id
    run_dir.mkdir(parents=True)
    _write(run_dir / "data.txt", b"some data")  # file present, but no manifest

    run_dirs = [(run_id, "proj-y", run_dir)]
    data = compute_campaign_merkle(campaign_dir, run_dirs)

    expected_sentinel = _sha256(b"MISSING_MANIFEST:" + run_id.encode())
    assert data["runs"][0]["manifest_sha256"] == expected_sentinel


def test_write_then_verify_campaign_merkle_ok(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    campaign_dir = tmp_path / "campaign-ok"
    campaign_dir.mkdir()

    run_dir_a = _make_run(projects_root, "proj-z", "run-1111", b"data_1")
    run_dir_b = _make_run(projects_root, "proj-z", "run-2222", b"data_2")

    run_dirs = [
        ("run-1111", "proj-z", run_dir_a),
        ("run-2222", "proj-z", run_dir_b),
    ]

    merkle_path = write_campaign_merkle(campaign_dir, run_dirs)
    assert merkle_path == campaign_dir / "merkle.json"
    assert merkle_path.exists()

    result = verify_campaign_merkle(campaign_dir, projects_root)
    assert result.valid
    assert result.status == "ok"
    assert result.mismatches == []


def test_verify_campaign_merkle_detects_corrupted_run(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    campaign_dir = tmp_path / "campaign-corrupt"
    campaign_dir.mkdir()

    run_dir_a = _make_run(projects_root, "proj-c", "run-aaaa", b"clean_data")
    run_dir_b = _make_run(projects_root, "proj-c", "run-bbbb", b"other_data")

    run_dirs = [
        ("run-aaaa", "proj-c", run_dir_a),
        ("run-bbbb", "proj-c", run_dir_b),
    ]

    write_campaign_merkle(campaign_dir, run_dirs)

    # Corrupt a source file in run-aaaa, then re-write its manifest
    # so the manifest_sha256 in merkle.json no longer matches the manifest on disk
    output_file = run_dir_a / "output.txt"
    output_file.write_bytes(b"tampered_data")
    write_run_manifest(run_dir_a, run_id="run-aaaa")  # manifest now reflects tampered state

    result = verify_campaign_merkle(campaign_dir, projects_root)
    assert result.status == "corrupted"
    assert "run-aaaa" in result.mismatches


def test_verify_campaign_merkle_missing_merkle_file(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "campaign-no-merkle"
    campaign_dir.mkdir()

    result = verify_campaign_merkle(campaign_dir, tmp_path / "projects")
    assert result.status == "missing"
    assert not result.valid


def test_verify_run_manifest_corrupt_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_bytes(b"not valid json {{{")
    result = verify_run_manifest(run_dir)
    assert result.status == "corrupted"
    # mismatch should mention the unparseable manifest
    assert any("unparseable" in m or "manifest.json" in m for m in result.mismatches)


def test_verify_campaign_merkle_corrupt_json(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "camp"
    campaign_dir.mkdir()
    (campaign_dir / "merkle.json").write_bytes(b"not valid json {{{")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    result = verify_campaign_merkle(campaign_dir, projects_root)
    assert result.status == "corrupted"


def test_compute_campaign_merkle_empty_runs(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "camp"
    campaign_dir.mkdir()
    result = compute_campaign_merkle(campaign_dir, [])
    assert result["merkle_root"] == hashlib.sha256(b"").hexdigest()
    assert result["runs"] == []
