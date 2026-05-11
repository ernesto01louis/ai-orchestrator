#!/usr/bin/env python3
"""Measure embedding-cache key persistence under a one-line edit.

The repo-screening chonkie spike (2026-05-11) lands a chunking primitive
in ``core/chunking.py`` but does not wire it into the live embedding
pipeline yet. The spike's success criterion is: when one line in a
document changes, does chunking preserve significantly more
cache-keyable units than the trivial whole-text baseline?

This script answers that question on a real corpus.

Usage::

    python scripts/measure_chunking_hit_rate.py
    python scripts/measure_chunking_hit_rate.py --corpus references/
    python scripts/measure_chunking_hit_rate.py --corpus references/ \
        --chunk-size 1024 --out /tmp/chunking-measurement.json

Output is a JSON blob with:
    - corpus: path measured
    - files_measured: count of files actually processed
    - chunker_config: chunk_size + chunk_overlap used
    - per-file results: {keys_before, keys_after, keys_persisted,
                         persistence_ratio, naive_persistence_ratio}
    - aggregate: average persistence ratio, win-vs-naive count

The script does NOT touch the orchestrator's live embedding cache and
does NOT modify the corpus files (mutates a copy in-memory).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# Allow running from the repo root without installing as a package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import chunking, config  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash(text: str) -> str:
    """SHA-256 hex of UTF-8 bytes — the same shape the embedding cache uses."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _one_line_edit(text: str) -> str | None:
    """Mutate one non-empty line in the middle of the document.

    Returns ``None`` when the document has fewer than 3 lines, in which
    case the edit-stability question is meaningless.
    """
    lines = text.splitlines(keepends=True)
    if len(lines) < 3:
        return None
    # Pick a line near the middle that has actual content.
    midpoint = len(lines) // 2
    for delta in range(0, len(lines) // 2):
        idx = midpoint + delta
        if idx < len(lines) and lines[idx].strip():
            lines[idx] = "EDITED MARKER " + lines[idx]
            return "".join(lines)
        idx = midpoint - delta
        if idx >= 0 and lines[idx].strip():
            lines[idx] = "EDITED MARKER " + lines[idx]
            return "".join(lines)
    return None


def _chunk_keys(text: str) -> set[str]:
    """Cache-key set for chonkie-chunked text. Caller sets enabled=True."""
    chunks = chunking.chunk_text(text, site="measurement")
    return {_hash(c) for c in chunks}


def _naive_whole_text_keys(text: str) -> set[str]:
    """The trivial baseline — one cache key for the whole document."""
    return {_hash(text)} if text else set()


_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def _read_text_safe(path: Path) -> str | None:
    """Read a text file; return None on binary / unreadable inputs."""
    try:
        text = path.read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return None
    # Strip leading YAML frontmatter — measuring chunk stability on
    # frontmatter-heavy notes overstates the win.
    text = _FRONTMATTER_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _iter_corpus(root: Path) -> list[Path]:
    """All .md / .txt files under root, sorted for reproducibility."""
    if root.is_file():
        return [root]
    found: list[Path] = []
    for ext in (".md", ".txt"):
        found.extend(root.rglob(f"*{ext}"))
    return sorted(found)


def _measure_file(path: Path, *, min_chars: int = 256) -> dict | None:
    text_a = _read_text_safe(path)
    if not text_a or len(text_a) < min_chars:
        return None
    text_b = _one_line_edit(text_a)
    if text_b is None:
        return None

    keys_chunked_a = _chunk_keys(text_a)
    keys_chunked_b = _chunk_keys(text_b)
    persisted_chunked = keys_chunked_a & keys_chunked_b

    keys_naive_a = _naive_whole_text_keys(text_a)
    keys_naive_b = _naive_whole_text_keys(text_b)
    persisted_naive = keys_naive_a & keys_naive_b

    return {
        "path": str(path),
        "chars": len(text_a),
        "chunks_before": len(keys_chunked_a),
        "chunks_after": len(keys_chunked_b),
        "chunks_persisted": len(persisted_chunked),
        "chunk_persistence_ratio": (
            len(persisted_chunked) / max(len(keys_chunked_a), 1)
        ),
        "naive_persistence_ratio": (
            len(persisted_naive) / max(len(keys_naive_a), 1)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", type=Path, default=Path("references"),
        help="Directory (or single file) to measure. Default: references/",
    )
    parser.add_argument("--chunk-size", type=int, default=1024)
    parser.add_argument("--chunk-overlap", type=int, default=128)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write full JSON to this path (default: stdout summary only).",
    )
    parser.add_argument(
        "--min-chars", type=int, default=256,
        help="Skip files smaller than this many characters.",
    )
    args = parser.parse_args()

    if not args.corpus.exists():
        print(f"[measure] corpus path {args.corpus} does not exist", file=sys.stderr)
        return 2

    # Force-enable the chunker for the measurement run. The script does
    # not write back to config.json.
    config.CHUNKING_ENABLED = True
    config.CHUNKING_CHUNK_SIZE = args.chunk_size
    config.CHUNKING_CHUNK_OVERLAP = args.chunk_overlap
    chunking._chunker = None  # rebuild with the requested chunk_size

    files = _iter_corpus(args.corpus)
    per_file: list[dict] = []
    skipped = 0
    for path in files:
        result = _measure_file(path, min_chars=args.min_chars)
        if result is None:
            skipped += 1
            continue
        per_file.append(result)

    if not per_file:
        print(
            f"[measure] no files measured (scanned={len(files)} skipped={skipped})",
            file=sys.stderr,
        )
        return 1

    chunk_ratios = [r["chunk_persistence_ratio"] for r in per_file]
    naive_ratios = [r["naive_persistence_ratio"] for r in per_file]
    wins = sum(1 for r in per_file if r["chunk_persistence_ratio"] > r["naive_persistence_ratio"])

    aggregate = {
        "files_scanned": len(files),
        "files_measured": len(per_file),
        "files_skipped": skipped,
        "mean_chunk_persistence": sum(chunk_ratios) / len(chunk_ratios),
        "mean_naive_persistence": sum(naive_ratios) / len(naive_ratios),
        "wins_vs_naive": wins,
        "win_rate": wins / len(per_file),
        "spike_criterion_met": (sum(chunk_ratios) / len(chunk_ratios)) > 0.5,
    }
    out = {
        "corpus": str(args.corpus),
        "chunker_config": {
            "chunker": config.CHUNKING_CHUNKER,
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
        },
        "aggregate": aggregate,
        "per_file": per_file,
    }

    if args.out:
        args.out.write_text(json.dumps(out, indent=2))
        print(f"[measure] wrote {args.out}")

    # Always print the summary line so the spike result is visible.
    print(json.dumps(aggregate, indent=2))
    return 0 if aggregate["spike_criterion_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
