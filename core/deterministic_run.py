"""Register a deterministic (non-LLM) run for citation-grade evidence.

A *deterministic run* executes a fully specified recipe with no LLM in the
loop — e.g. a CFD campaign whose bash recipe is fixed in the campaign YAML
and run directly over SSH. The orchestrator's planner/generator/judge
pipeline is bypassed, so ``LLM_CALL_LOG`` is empty for the run.

That emptiness is **correct, not degraded**. The citation-grade provenance
of such a run is its git SHA, input parameters, solver version, output-file
SHA-256 hashes, per-run manifest, campaign Merkle root and DSSE signature —
none of which need an LLM. The evidence builder already handles an empty
``llm_calls[]`` gracefully; the only thing missing was the *plumbing* to
write a deterministic run into the on-disk layout the builder expects.

``register_deterministic_run`` writes that layout. Afterwards
``evidence.builder.build_bundle(campaign_id)`` and the whole
evidence / manifest / signing stack work unchanged, and the bundle is
honestly stamped ``provenance_mode="deterministic"`` so a reader knows the
empty LLM trace is intentional.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import PROJECTS_DIR
from manifest.run_manifest import write_run_manifest
from memory_pkg import load_campaigns, save_campaigns

# CampaignTemplate requires planner/generator/judge model fields. A
# deterministic run has none — this sentinel makes that explicit in the
# campaign record and the resulting evidence bundle's llm_targets.
_NO_LLM = "none (deterministic recipe — no LLM)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n")


def register_deterministic_run(
    *,
    campaign_id: str,
    campaign_name: str,
    hypothesis: str,
    project_name: str,
    deploy_target: str,
    run_id: str,
    params: dict[str, Any],
    recipe: str,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    score: float = 0.0,
    duration_ms: int = 0,
    solver: str = "",
) -> Path:
    """Write a deterministic run into the evidence-builder's on-disk layout.

    Creates the campaign record in ``memory/campaigns.json`` on first call
    (extends it — appending/replacing the run — on later calls), and writes
    ``projects/<project_name>/runs/<run_id>/`` with ``plan.json`` (stamped
    ``provenance_mode="deterministic"``), ``execution.json``,
    ``environment.json``, ``files.json``, ``score.txt``, ``prompt.txt``,
    ``src/run.sh`` and a per-run ``manifest.json``.

    Args:
        campaign_id: stable id for the campaign these runs belong to.
        campaign_name / hypothesis: campaign-level metadata (hypothesis is
            REFORMS §1 pre-registration — required by the bundle).
        project_name: the orchestrator project key (``projects/<name>/``).
        deploy_target: host the recipe ran on (recorded, not contacted).
        run_id: unique id for this run.
        params: this run's point in the parameter sweep.
        recipe: the exact shell recipe that was executed.
        stdout / stderr / returncode / duration_ms: real execution output.
        score: numeric score (0.0 if not scored).
        solver: solver + version string, e.g. "OpenFOAM v2412".

    Returns:
        The run directory path.
    """
    run_dir = Path(PROJECTS_DIR) / project_name / "runs" / run_id
    src_dir = run_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    recipe_text = recipe if recipe.endswith("\n") else recipe + "\n"
    (src_dir / "run.sh").write_text(recipe_text)
    (run_dir / "prompt.txt").write_text(recipe_text)
    (run_dir / "score.txt").write_text(str(score))

    _write_json(run_dir / "plan.json", {
        "language": "bash",
        "entrypoint": "run.sh",
        "project_type": "script",
        "execution_mode": "deterministic",
        "files": {"run.sh": "deterministic recipe — fixed, no LLM generation"},
        "dependencies": [],
        "steps": [],
        # read by evidence.builder._read_provenance_mode
        "provenance_mode": "deterministic",
    })
    _write_json(run_dir / "execution.json", {
        "language": "bash",
        "stdout": stdout,
        "stderr": stderr,
        "returncode": int(returncode),
        "duration_ms": int(duration_ms),
    })
    _write_json(run_dir / "environment.json", {
        "provenance_mode": "deterministic",
        "deploy_target": deploy_target,
        "solver": solver,
        "registered_at": _now_iso(),
    })
    _write_json(run_dir / "files.json", {"run.sh": recipe_text})

    # Phase 1.5 per-run SHA-256 manifest — pure compute over the files above.
    write_run_manifest(run_dir, run_id=run_id)

    # --- campaign record so evidence.builder.build_bundle can load it ---
    campaigns = load_campaigns()
    status = "success" if int(returncode) == 0 else "fail"
    run_entry = {
        "run_id": run_id,
        "params": dict(params),
        "status": status,
        "score": float(score),
    }
    now = _now_iso()
    if campaign_id in campaigns:
        camp = campaigns[campaign_id]
        runs = [r for r in camp.get("runs", []) if r.get("run_id") != run_id]
        runs.append(run_entry)
        camp["runs"] = runs
        camp["updated_at"] = now
        camp["completed_at"] = now
        # accumulate this run's params into the campaign sweep grid
        grid = camp.setdefault("params", {})
        for key, val in params.items():
            grid.setdefault(key, [])
            if val not in grid[key]:
                grid[key].append(val)
    else:
        campaigns[campaign_id] = {
            "name": campaign_name,
            "description": "Deterministic campaign — recipe-driven runs, no LLM pipeline.",
            "hypothesis": hypothesis,
            "template": {
                "project_name": project_name,
                "prompt": recipe_text,
                "planner_model": _NO_LLM,
                "generator_models": [_NO_LLM],
                "judge_model": _NO_LLM,
                "deploy_target": deploy_target,
            },
            "params": {key: [val] for key, val in params.items()},
            "max_runs": None,
            "parallelism": 1,
            "id": campaign_id,
            "status": "completed",
            "runs": [run_entry],
            "created_at": now,
            "updated_at": now,
            "completed_at": now,
        }
    save_campaigns(campaigns, changed_ids=[campaign_id])
    return run_dir
