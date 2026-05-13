"""Phase 2 hardening — regression test for agent schema single-sourcing.

The previous Phase 0 pattern carried inline fallback dicts in both
``app.py`` (lines ~326–358) and ``orchestration/__init__.py``
(lines ~123–153) and silently fell back to whichever copy if
``agents/<role>/schema.json`` failed to load. Those inline copies
drifted from the canonical files on disk (planner gained
``project_type`` / ``execution_mode`` / ``port`` / ``steps``; judge
swapped scalar ``score`` for multi-dimensional scoring; tool_dispatch
renamed ``tools_to_run`` → ``tools``). Activating the stale fallback
on a corrupted ``agents/`` directory broke structured-output validation
in subtly wrong ways.

This test locks in the single-source contract:

1. The canonical schemas on disk parse cleanly.
2. ``agents.loader.load_schema(role)`` returns the same JSON as the
   on-disk file (byte-identical content + Python dict equality).
3. ``orchestration.PLAN_SCHEMA`` / ``JUDGE_SCHEMA`` /
   ``TOOL_DISPATCH_SCHEMA`` are exactly what ``load_schema`` returns
   for their respective roles (no copying / mutation in between).
4. ``load_schema`` raises ``RuntimeError`` when the schema is missing
   or invalid (fail-fast contract).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.loader import load_schema

SCHEMA_ROLES = ("planner", "judge", "tool_dispatch")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _schema_path(role: str) -> Path:
    return REPO_ROOT / "agents" / role / "schema.json"


@pytest.mark.parametrize("role", SCHEMA_ROLES)
def test_canonical_schema_file_exists_and_parses(role: str) -> None:
    """Each role with a structured-output contract has a parseable schema."""
    path = _schema_path(role)
    assert path.is_file(), f"missing canonical schema: {path}"
    data = json.loads(path.read_text())
    assert isinstance(data, dict), f"{path} must be a JSON object at the top level"
    assert data.get("type") == "object", f"{path} top-level type must be 'object'"
    assert "properties" in data, f"{path} missing 'properties'"
    assert "required" in data, f"{path} missing 'required'"


@pytest.mark.parametrize("role", SCHEMA_ROLES)
def test_load_schema_matches_on_disk(role: str) -> None:
    """``load_schema`` returns the same dict as reading the file directly.

    Catches drift between the loader and the canonical schema file —
    e.g. a future caching layer that returns stale content, or an
    accidental mutation of the loader's return value.
    """
    loaded = load_schema(role)
    on_disk = json.loads(_schema_path(role).read_text())
    assert loaded == on_disk, (
        f"load_schema({role!r}) returned a dict that doesn't match "
        f"{_schema_path(role)} byte-for-byte"
    )


@pytest.mark.parametrize("role", SCHEMA_ROLES)
def test_orchestration_module_exports_loaded_schema(role: str) -> None:
    """Module-level constants in ``orchestration`` are exactly what
    ``load_schema`` returns. Detects any future refactor that
    re-introduces inline literals or mutates the loaded dict.
    """
    import orchestration

    constant_name = {
        "planner": "PLAN_SCHEMA",
        "judge": "JUDGE_SCHEMA",
        "tool_dispatch": "TOOL_DISPATCH_SCHEMA",
    }[role]
    module_constant = getattr(orchestration, constant_name)
    assert module_constant == load_schema(role)


def test_load_schema_raises_on_missing_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-source contract: a missing schema is a deployment-level
    corruption, not a "silently fall back to a stale dict" situation."""
    import agents.loader as loader_mod

    monkeypatch.setattr(loader_mod, "AGENTS_DIR", str(tmp_path))
    role_dir = tmp_path / "fake_role"
    role_dir.mkdir()
    # Intentionally don't create schema.json

    with pytest.raises(RuntimeError, match=r"schema\.json missing"):
        load_schema("fake_role")


def test_load_schema_raises_on_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed schema is also a hard fail, not a fallback."""
    import agents.loader as loader_mod

    monkeypatch.setattr(loader_mod, "AGENTS_DIR", str(tmp_path))
    role_dir = tmp_path / "broken_role"
    role_dir.mkdir()
    (role_dir / "schema.json").write_text("{ this is not json }")

    with pytest.raises(RuntimeError, match=r"schema\.json invalid"):
        load_schema("broken_role")


def test_app_module_does_not_redefine_schemas() -> None:
    """Regression guard against the old triple-source pattern returning
    via copy-paste. ``app.py`` must NOT define its own PLAN_SCHEMA /
    JUDGE_SCHEMA / TOOL_DISPATCH_SCHEMA — the canonical exports live in
    ``orchestration``.
    """
    source = (REPO_ROOT / "app.py").read_text()
    # Use word boundaries to allow matching identifier-only occurrences
    # without false-positives from the explanatory comment block.
    forbidden_assignments = (
        "PLAN_SCHEMA = _load_agent_schema",
        "JUDGE_SCHEMA = _load_agent_schema",
        "TOOL_DISPATCH_SCHEMA = _load_agent_schema",
        "def _load_agent_schema",
    )
    for token in forbidden_assignments:
        assert token not in source, (
            f"app.py must not re-define agent schemas — found {token!r}. "
            "Single source is orchestration.PLAN_SCHEMA et al."
        )


def test_inline_fallback_dicts_are_absent_from_orchestration() -> None:
    """The orchestration module must not carry inline fallback dict
    literals for the three structured agents. If a future change
    re-introduces them, the on-disk schemas can silently drift.
    """
    source = (REPO_ROOT / "orchestration" / "__init__.py").read_text()
    forbidden_substrings = (
        '"tools_to_run"',      # old tool_dispatch shape
        '"score": {"type": "integer"}',  # old judge shape
        '"approach"',           # old planner shape
    )
    for token in forbidden_substrings:
        assert token not in source, (
            f"orchestration/__init__.py contains the old inline-fallback "
            f"shape ({token!r}). Schemas must come exclusively from "
            f"agents/<role>/schema.json via agents.loader.load_schema."
        )


