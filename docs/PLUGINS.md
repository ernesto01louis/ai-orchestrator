# Plugins

> Optional integrations carved out of the base install so the
> orchestrator stays platform-neutral. See
> [VISION.md](../VISION.md) for the "platform not hub" principle.
> Each plugin ships its own requirements file plus a config block
> in ``config.example.json``; the orchestrator runs fine with none
> of them installed.

## Pattern

Four optional surfaces today, each independently installable:

| Plugin | Install | Config flag | Status |
|---|---|---|---|
| [Blender MCP](#blender-mcp) | ``pip install -r requirements-plugins.txt`` | ``mcp_servers.blender`` | dormant |
| [Cloud image gen](#cloud-image-gen-replicate--gemini) | (no extra wheel; uses ``requests``) | ``cloud_image_gen.enabled`` | dormant |
| [Deepeval (G-Eval)](#deepeval-g-eval) | ``pip install -r requirements-eval.txt`` | ``eval.enabled`` | dormant |
| [SkyPilot cloud-burst](#skypilot-cloud-burst) | ``pip install -r requirements-cloud.txt`` | ``sky.enabled`` | dormant |

Activation pattern is consistent across all four:

1. Install the relevant ``requirements-*.txt``.
2. Flip the ``enabled: true`` flag in ``config.json`` (operators copy
   from ``config.example.json`` on first run).
3. Provide any required credentials in ``.env`` (or ``.env.sops``
   after the Phase 0 SOPS bootstrap — see [SOPS.md](SOPS.md)).
4. Restart ``ai-orchestrator.service``.

If a plugin's config block isn't present in ``config.json``, the
Pydantic settings layer falls back to ``enabled=false`` defaults.
Plugins fail-open: missing wheels / missing creds / unreachable
upstream all degrade to no-op without affecting orchestration.

---

## Blender MCP

**Wheel:** ``blender-mcp==1.5.6``
**Source:** [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)
**Lives at:** ``requirements-plugins.txt``

Drives Blender's Python API via an MCP tool surface — consumer
projects rendering 3D scenes from LLM-generated geometry can call
into Blender from inside the orchestration loop. The orchestrator
itself does NOT import ``blender-mcp`` at runtime; it's only useful
when a consumer project registers Blender as an MCP target.

**Install (operator):**

```bash
pip install -r requirements-plugins.txt
# Plus: stand up a Blender instance reachable from the orchestrator
# LXC. Typical pattern is Blender headless on a render box; see the
# blender-mcp README for the server-side setup.
```

**Config:** add an entry to ``mcp_servers`` in ``config.json``:

```json
"mcp_servers": {
  "blender": {
    "command": "uv",
    "args": ["run", "blender-mcp"]
  }
}
```

The Phase 1.7 MCP contract auto-discovers servers from this map.

---

## Cloud image gen (Replicate + Gemini)

**Wheel:** none (uses ``requests`` already in base ``requirements.txt``)
**Lives at:** ``config.example.json:cloud_image_gen``

Replicate (FLUX) primary, Gemini fallback. The orchestrator calls
this from consumer projects that need synthesised imagery (figure
captions, diagrams, brand assets, etc.). Genuinely useful for some
operators; pure dead weight for others.

**Config:** flip ``cloud_image_gen.enabled = true`` in ``config.json``,
drop ``REPLICATE_API_TOKEN`` and ``GEMINI_API_KEY`` into ``.env``,
restart the service.

```json
"cloud_image_gen": {
  "enabled": true,
  "provider": "replicate",
  "default_model": "black-forest-labs/flux-dev",
  "fallback": {
    "provider": "gemini",
    "model": "gemini-2.5-flash-image"
  }
}
```

Safe to delete the entire block if you're not using image
generation. Pydantic defaults fill it in as disabled.

---

## Deepeval (G-Eval)

**Wheel:** ``deepeval`` + transitive deps (~14 packages)
**Source:** [confident-ai/deepeval](https://github.com/confident-ai/deepeval)
**Lives at:** ``requirements-eval.txt``

Repo-screening spike landed dormant ([eval_pkg/scoring.py](../eval_pkg/scoring.py)
+ [scripts/measure_eval_quality.py](../scripts/measure_eval_quality.py)).
``score_response(input, output, criteria)`` wraps deepeval's G-Eval
with an Ollama judge so consumers can score their LLM outputs
against curated test suites. NOT wired into the live planner /
generator / judge pipeline.

**Install (operator):**

```bash
pip install -r requirements-eval.txt
```

**Config:** flip ``eval.enabled = true``; pick a judge model in
``eval.judge_model`` (defaults to ``llama3:8b``).

**Recommended usage:** a downstream "eval campaign" type that runs
``score_response`` against curated prompts with known-good answers;
the harness at ``scripts/measure_eval_quality.py`` is the canonical
example.

---

## SkyPilot cloud-burst

**Wheel:** ``skypilot[runpod,vast]>=0.12,<0.13``
**Source:** [skypilot-org/skypilot](https://github.com/skypilot-org/skypilot)
**Lives at:** ``requirements-cloud.txt``

Phase 2.5 cloud-burst scaffold. Spins up rented GPU clusters (RunPod
/ Vast.ai today) for short bursts of heavy LLM work; integrates with
Phase 2.4 budget tracking for cost accrual. Three-tier cost
discipline (per-burst ceiling at launch + per-campaign budget at
accrual + idle-stop daemon).

**Install (operator):**

```bash
pip install -r requirements-cloud.txt
# Drop provider creds:
# - RunPod:  ~/.runpod/api_key.toml  (see RUNBOOK § "SkyPilot")
# - Vast.ai: vastai set api-key XXXX
sky check  # confirms creds are wired
```

**Config:** flip ``sky.enabled = true`` and (optionally) tune
``sky.idle_timeout_minutes`` + ``sky.max_burst_cost_usd``. See
[ROADMAP.md](../ROADMAP.md) § 2.5 for the burst-and-stop smoke test
recipe.

Once activated, ``POST /runs/{id}/burst`` launches a SkyPilot job
attached to a specific orchestrator run; the idle-stop daemon polls
every 60s and tears down clusters past the timeout.

---

## When to add a new plugin

Anything that:

1. Imports a third-party wheel the orchestrator itself doesn't need
   at runtime, **AND**
2. Is useful to fewer than ~3 hypothetical consumer projects, **OR**
3. References a specific domain (3D rendering, image gen, hardware
   simulators, scientific solvers, …)

belongs here, not in the base install. Pattern:

1. Add a ``requirements-<plugin>.txt`` file with the wheel pinned.
2. Add a config block to ``config.example.json`` annotated
   ``"_comment": "Optional plugin — see docs/PLUGINS.md..."`` and an
   ``enabled: false`` default.
3. Add a Pydantic config class in ``core/config_schema.py`` so the
   settings layer accepts the block.
4. Wire any orchestrator-side code behind an ``is_enabled()`` gate
   that fails open when the wheel is missing.
5. Add a section to this doc.
6. Update [CLAUDE.md](../CLAUDE.md) "Do NOT build" list if the
   plugin's scope creeps toward "this should be in core".

The "platform not hub" test:

> Would an unrelated research project (protein folding, algorithmic
> trading, music generation) also benefit from this change?

If "no" → plugin. If "yes" → it's not a plugin, it's a platform
feature; add it to the core surface with full tests.
