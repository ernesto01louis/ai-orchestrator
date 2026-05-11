"""Chonkie-backed text chunking primitive.

Repo-screening spike (2026-05-11). Ships dormant: ``is_enabled()``
returns ``False`` until ``chunking.enabled=true`` lands in
``config.json``. Even when enabled, no orchestrator callsite wires
chunking in yet — embeddings are still computed on whole texts in
``memory_pkg`` and references are still loaded whole in
``references_pkg``. Promoting chunking into the embedding pipeline is
a separate phase (changes cache-key semantics across the codebase).

The point of this module is to land the primitive + measurement so the
"does recursive chunking preserve cache keys under one-line edits?"
question can be answered without disturbing the live pipeline.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core import config as _config
from core.metrics import observe_chunking

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


# Lazy module-level cache — chonkie's RecursiveChunker constructs a
# tokenizer (gpt2 BPE by default) which is non-trivial. Build once.
_chunker: object | None = None


def is_enabled() -> bool:
    """Three-condition gate: config flag + chonkie importable.

    Mirrors the Phase 3.3 ``note_discovery.is_enabled`` shape — a single
    boolean every callsite checks before doing any work.
    """
    if not _config.CHUNKING_ENABLED:
        return False
    try:
        import chonkie  # noqa: F401
    except ImportError:
        return False
    return True


def _get_chunker() -> object:
    """Construct (or return cached) chonkie chunker per CHUNKING_* config."""
    global _chunker
    if _chunker is not None:
        return _chunker
    from chonkie import RecursiveChunker

    variant = _config.CHUNKING_CHUNKER
    if variant != "recursive":
        # The config schema only declares ``recursive`` today; any other
        # value is a config error. Fail loudly rather than silently
        # falling back, so misconfiguration surfaces.
        raise ValueError(
            f"core.chunking: unsupported chunker variant {variant!r}; "
            "only 'recursive' is wired today"
        )
    _chunker = RecursiveChunker(chunk_size=_config.CHUNKING_CHUNK_SIZE)
    return _chunker


def chunk_text(text: str, *, site: str = "unspecified") -> list[str]:
    """Chunk a text into a list of text-only strings.

    Returns a single-element list ``[text]`` when chunking is disabled or
    when the text is empty — keeps callers from having to special-case
    the dormant path.

    ``site`` is a low-cardinality label for the Prometheus counter
    (``orchestrator_chunking_chunks_total{site,chunker}``). Reuse one of
    {"references", "vault", "measurement"} or add a new value in
    ``core.metrics`` first.
    """
    if not text:
        return []
    if not is_enabled():
        # Dormant path — return the whole text as a single chunk so
        # callers can use ``chunk_text(...)`` unconditionally.
        return [text]

    chunker = _get_chunker()
    chunks = chunker(text)  # type: ignore[operator]
    out = [c.text for c in chunks]
    observe_chunking(site=site, chunker=_config.CHUNKING_CHUNKER, count=len(out))
    return out


def chunk_texts(texts: Iterable[str], *, site: str = "unspecified") -> list[str]:
    """Flatten-chunk an iterable of texts. Convenience wrapper."""
    out: list[str] = []
    for t in texts:
        out.extend(chunk_text(t, site=site))
    return out
