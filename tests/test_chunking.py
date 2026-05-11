"""Tests for core/chunking.py — repo-screening chonkie spike.

Covers:
- Dormant default (CHUNKING_ENABLED=False) returns a single-element list.
- is_enabled() three-condition gate (config + import probe).
- Empty text path.
- Enabled path produces multiple chunks for long text.
- The spike's actual question: chunk-key stability under a one-character
  edit. RecursiveChunker should preserve most chunks intact when one
  far-away line changes.
- Prom counter bumps with the expected (site, chunker) label.
"""
from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import patch

import pytest

from core import chunking, config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash(text: str) -> str:
    """Stable cache-key proxy for the measurement test."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _long_text(n_paragraphs: int = 20) -> str:
    """A multi-paragraph corpus the recursive chunker can split on."""
    para = (
        "The orchestrator routes language model calls through a 5-layer "
        "memory system. Layer one is identity. Layer two is primer. "
        "Layer three is live context. Layer four is Hindsight. Layer five "
        "is the Obsidian vault, syncthing-replicated to NAS."
    )
    return "\n\n".join(f"Paragraph {i}. {para}" for i in range(n_paragraphs))


@pytest.fixture(autouse=True)
def _reset_chunker_cache() -> Any:
    """Clear the lazy module-level chunker between tests."""
    chunking._chunker = None
    saved = (
        config.CHUNKING_ENABLED,
        config.CHUNKING_CHUNKER,
        config.CHUNKING_CHUNK_SIZE,
        config.CHUNKING_CHUNK_OVERLAP,
    )
    yield
    (
        config.CHUNKING_ENABLED,
        config.CHUNKING_CHUNKER,
        config.CHUNKING_CHUNK_SIZE,
        config.CHUNKING_CHUNK_OVERLAP,
    ) = saved
    chunking._chunker = None


# ---------------------------------------------------------------------------
# Dormant + gate behaviour
# ---------------------------------------------------------------------------

def test_dormant_returns_single_chunk() -> None:
    """When disabled, chunk_text returns the whole text as one chunk.

    Callers can use chunk_text(...) unconditionally without branching
    on the config flag.
    """
    config.CHUNKING_ENABLED = False
    assert chunking.chunk_text("hello world") == ["hello world"]


def test_empty_text_returns_empty_list() -> None:
    """Empty input bypasses the gate and returns an empty list."""
    config.CHUNKING_ENABLED = True
    assert chunking.chunk_text("") == []


def test_is_enabled_respects_config_flag() -> None:
    config.CHUNKING_ENABLED = False
    assert chunking.is_enabled() is False
    config.CHUNKING_ENABLED = True
    assert chunking.is_enabled() is True


def test_is_enabled_handles_missing_import() -> None:
    """If chonkie isn't installed, is_enabled returns False even when
    the config flag is on. Mirrors the Phase 3.3 NoteDiscovery shape."""
    config.CHUNKING_ENABLED = True

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "chonkie":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_fake_import):
        assert chunking.is_enabled() is False


# ---------------------------------------------------------------------------
# Enabled path — real chonkie call
# ---------------------------------------------------------------------------

def test_enabled_long_text_produces_multiple_chunks() -> None:
    config.CHUNKING_ENABLED = True
    config.CHUNKING_CHUNK_SIZE = 256
    chunks = chunking.chunk_text(_long_text(), site="measurement")
    assert len(chunks) > 1
    # Round-trip — concatenating chunks should contain the original content
    # (recursive chunker may add no glue, but every paragraph should land
    # in some chunk).
    joined = "".join(chunks)
    assert "Paragraph 0." in joined
    assert "Paragraph 19." in joined


def test_unsupported_chunker_variant_raises() -> None:
    config.CHUNKING_ENABLED = True
    config.CHUNKING_CHUNKER = "neural"
    with pytest.raises(ValueError, match="unsupported chunker variant"):
        chunking.chunk_text("hello world")


# ---------------------------------------------------------------------------
# The spike's actual question — chunk stability under one-character edit
# ---------------------------------------------------------------------------

def test_chunk_stability_under_one_line_edit() -> None:
    """Edit one paragraph; >50% of chunk-hash keys should persist.

    This is the spike's success criterion. If RecursiveChunker can't beat
    naive whole-text hashing on this benchmark, the technique doesn't
    win for orchestrator-shaped corpora and we don't promote it past
    "available primitive."
    """
    config.CHUNKING_ENABLED = True
    config.CHUNKING_CHUNK_SIZE = 256

    text_a = _long_text(n_paragraphs=20)
    # Tweak one paragraph in the middle so chunks at the head and tail
    # ought to survive.
    text_b = text_a.replace("Paragraph 10.", "Paragraph 10 (edited).", 1)

    chunks_a = chunking.chunk_text(text_a, site="measurement")
    chunks_b = chunking.chunk_text(text_b, site="measurement")

    keys_a = {_hash(c) for c in chunks_a}
    keys_b = {_hash(c) for c in chunks_b}

    persisted = keys_a & keys_b
    # With 20 paragraphs and one edit, far more than half of the chunk
    # boundaries should be undisturbed.
    persistence_ratio = len(persisted) / max(len(keys_a), 1)
    assert persistence_ratio > 0.5, (
        f"persistence={persistence_ratio:.2f} chunks_a={len(keys_a)} "
        f"chunks_b={len(keys_b)} persisted={len(persisted)}"
    )


def test_chunk_stability_baseline_naive_whole_text() -> None:
    """Sanity check the comparison: naive whole-text hashing scores 0 on
    the same one-character edit. Confirms our metric measures the right
    thing (chunking wins only if it beats this trivial baseline)."""
    text_a = _long_text(n_paragraphs=20)
    text_b = text_a.replace("Paragraph 10.", "Paragraph 10 (edited).", 1)
    assert _hash(text_a) != _hash(text_b)


# ---------------------------------------------------------------------------
# Prom counter
# ---------------------------------------------------------------------------

def test_chunk_text_bumps_prom_counter() -> None:
    from core import metrics

    config.CHUNKING_ENABLED = True
    config.CHUNKING_CHUNK_SIZE = 256

    counter = metrics.CHUNKING_CHUNKS_TOTAL.labels(
        site="measurement", chunker="recursive",
    )
    before = counter._value.get()
    chunks = chunking.chunk_text(_long_text(), site="measurement")
    after = counter._value.get()
    assert after - before == len(chunks)


def test_chunk_text_dormant_does_not_bump_counter() -> None:
    from core import metrics

    config.CHUNKING_ENABLED = False
    counter = metrics.CHUNKING_CHUNKS_TOTAL.labels(
        site="measurement", chunker="recursive",
    )
    before = counter._value.get()
    chunking.chunk_text("hello world", site="measurement")
    after = counter._value.get()
    assert after == before


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def test_chunk_texts_flattens_iterable() -> None:
    config.CHUNKING_ENABLED = True
    config.CHUNKING_CHUNK_SIZE = 256
    out = chunking.chunk_texts([_long_text(5), _long_text(5)], site="measurement")
    # Two long inputs should each produce >=1 chunk; total > 1.
    assert len(out) >= 2
