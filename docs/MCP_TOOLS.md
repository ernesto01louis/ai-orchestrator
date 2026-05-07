# MCP Contract — AI Orchestrator

**Contract version:** 1.0.0
**Mount point:** `/mcp` (Streamable HTTP, ASGI sub-app under FastAPI)
**Auth:** when `ORCHESTRATOR_API_TOKEN` is set on the server, requests must
carry `Authorization: Bearer <token>` (RFC 6750). When unset, auth is
disabled. See [RUNBOOK.md](../RUNBOOK.md) for the full setup.

The version field comes from `mcp_server.MCP_CONTRACT_VERSION`. The
machine-readable mirror of this document is the MCP resource
`orchestrator://contract` — it introspects the FastMCP managers at call
time, so docs and runtime can drift but the resource cannot.

## Versioning

This contract follows semver:

- **MAJOR** — a tool/resource/prompt is **removed or renamed**, an
  argument changes type or is removed, an existing return shape changes.
  External clients pinned to the previous major must be updated.
- **MINOR** — a tool/resource/prompt is **added**, an argument gains an
  optional default, a return field is added. Old clients keep working.
- **PATCH** — descriptions or docs change with zero behavioral effect.

Bump the constant in `mcp_server.py` in the same commit as the change.

## Discovery

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async with streamablehttp_client("http://orchestrator:8000/mcp/") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        resources = await session.list_resources()
        prompts = await session.list_prompts()
```

For a single-shot snapshot:

```python
contract = await session.read_resource("orchestrator://contract")
```

## Per-tool metadata

Every tool advertises both standard MCP `ToolAnnotations` (a hint to the
client) and an orchestrator-specific freeform `meta` dict.

**Annotations** — boolean hints, MCP-standard:

| Field             | Meaning                                                    |
|-------------------|------------------------------------------------------------|
| `readOnlyHint`    | Tool only reads orchestrator state.                        |
| `destructiveHint` | Tool can destroy or overwrite irretrievable state.         |
| `idempotentHint`  | Calling N times produces the same effect as calling once.  |
| `openWorldHint`   | Tool reaches outside the orchestrator process (SSH, HTTP). |

**`meta`** — orchestrator-specific, freeform JSON:

| Field              | Type   | Meaning                                                  |
|--------------------|--------|----------------------------------------------------------|
| `category`         | string | One of `orchestration`, `memory`, `ops`, `agent_config`. |
| `requires_target`  | bool   | Tool needs a `deploy_target` argument referring to an SSH-reachable device.   |

External clients can read these fields from each entry in
`orchestrator://contract`'s `tools` array, or via the MCP-standard
`tools/list` request.

## Tools

### `orchestrate`

Submit an orchestration job. Generates, tests, judges, and deploys code to a
target device. Spawns a background thread and returns immediately with a
`run_id` for polling.

**Category:** orchestration
**Annotations:** openWorldHint
**Requires target:** yes

| Argument | Type | Default | Description |
|---|---|---|---|
| `project_name` | `str` | required | Name for the project (used as deploy directory name) |
| `prompt` | `str` | required | What to build — natural language task description |
| `deploy_target` | `str` | required | Target device name (e.g. `pi-1`, `pi-2`, `Rak`) |
| `planner_model` | `str` | `"qwen2.5:72b"` | Model for planning |
| `generator_models` | `str` | `"qwen2.5-coder:32b"` | Comma-separated model names for code generation |
| `judge_model` | `str` | `"qwen2.5:72b"` | Model for code review/scoring |
| `optimizer_model` | `str` | `""` | Model for optimization pass (empty to skip) |
| `troubleshooter_model` | `str` | `""` | Model for fixing runtime errors (empty to skip) |
| `max_iterations` | `int` | `3` | Max generate-judge-optimize loops |

**Returns:**

```json
{
  "run_id": "<uuid>",
  "status": "started",
  "message": "Orchestration started for '...' on .... Use get_run_status to poll progress."
}
```

On invalid `deploy_target`:

```json
{ "error": "<message>" }
```

---

### `get_run_status`

Check the status of an orchestration run.

**Category:** orchestration
**Annotations:** readOnlyHint, idempotentHint
**Requires target:** no

| Argument | Type | Default | Description |
|---|---|---|---|
| `run_id` | `str` | required | The run ID returned by `orchestrate` |

**Returns:** the run status dict (internal `_`-prefixed keys filtered out),
or `{"error": "Run <id> not found"}` if the ID is unknown.

Key fields present while running: `phase`, `completed` (bool), `project`,
`target`. On completion: `result` (nested), `completed: true`.

---

### `get_run_result`

Get the final result of a completed orchestration run, including generated
files and scores.

**Category:** orchestration
**Annotations:** readOnlyHint, idempotentHint
**Requires target:** no

| Argument | Type | Default | Description |
|---|---|---|---|
| `run_id` | `str` | required | The run ID to get results for |

**Returns:**
- If not found: `{"error": "Run <id> not found"}`
- If still running: `{"status": "still running", "phase": "<phase>"}`
- If completed with error: `{"error": "<message>"}`
- If completed successfully: the `result` dict from the run status (scores,
  files, deploy path, etc.)

---

### `list_targets`

List all available deploy targets (Raspberry Pi devices) with their SSH
details.

**Category:** ops
**Annotations:** readOnlyHint, idempotentHint
**Requires target:** no

No arguments.

**Returns:**

```json
{
  "targets": [
    { "name": "pi-1", "host": "192.168.x.x", "username": "pi" },
    ...
  ]
}
```

---

### `list_models`

List all Ollama models currently available across all LXC containers.
Refreshes the URL cache before returning.

**Category:** ops
**Annotations:** readOnlyHint, idempotentHint, openWorldHint
**Requires target:** no

No arguments.

**Returns:**

```json
{
  "models": {
    "qwen2.5:72b": { "server": "http://192.168.x.x:11434" },
    ...
  }
}
```

---

### `run_dream_cycle`

Trigger a Dream memory consolidation cycle. Cleans, deduplicates, and
optimizes the orchestrator's memory layers.

**Category:** memory
**Annotations:** (none set — mutates memory state)
**Requires target:** no

No arguments.

**Returns:** a health report dict from `dream.run_dream`, including
`health_score`, `health_rating`, and per-layer statistics.

---

### `add_safety_gate`

Add a manual safety gate rule that blocks or warns on matching commands.

**Category:** ops
**Annotations:** (none set — mutates gate state)
**Requires target:** no

| Argument | Type | Default | Description |
|---|---|---|---|
| `pattern` | `str` | required | Regex pattern to match against tool commands |
| `reason` | `str` | required | Why this pattern should be blocked |
| `severity` | `str` | `"block"` | `"block"` (hard stop) or `"warn"` (log but allow) |

**Returns:** the gate dict as stored by `gates.add_gate` (includes `pattern`,
`reason`, `source`, `severity`, `created_at`).

---

### `reload_agents`

Reload all agent configurations from the `agents/` folder. Use after editing
prompt templates or `agent.yaml` files.

**Category:** agent_config
**Annotations:** idempotentHint
**Requires target:** no

No arguments.

**Returns:**

```json
{ "reloaded": ["planner", "generator", "judge", ...] }
```

---

### `update_agent_prompt`

Update a prompt template for an agent role. Changes take effect on the next
orchestration run.

**Category:** agent_config
**Annotations:** idempotentHint
**Requires target:** no

| Argument | Type | Default | Description |
|---|---|---|---|
| `role` | `str` | required | Agent role name (`planner`, `generator`, `judge`, `optimizer`, `troubleshooter`, `tool_dispatch`) |
| `prompt_type` | `str` | required | Which prompt to update: `system_prompt`, `user_prompt`, or `user_prompt_multi` |
| `content` | `str` | required | New prompt template content (supports `{{variable}}` placeholders) |

**Returns:**
- On success: `{"updated": "<prompt_type>", "role": "<role>"}`
- On invalid `prompt_type`: `{"error": "prompt_type must be one of: ..."}`
- On unknown role: `{"error": "Agent role '<role>' not found"}`

---

## Resources

Static resources return text content (JSON unless noted). Read them via
`session.read_resource(uri)`.

### `orchestrator://health`

Current memory health score and system status, drawn from the most recent
Dream cycle log entry.

**Shape:**

```json
{
  "health_score": 0.87,
  "health_rating": "good",
  "last_dream": "2026-05-07T06:00:00Z"
}
```

---

### `orchestrator://identity`

The orchestrator's identity and core principles (Layer 1 memory, `identity.md`).
Returns raw markdown text.

---

### `orchestrator://primer`

Current session state — active project, recent runs, blockers (Layer 2
memory, `primer.md`). Returns raw markdown text. Returns
`"No primer available."` if the file has not been written yet.

---

### `orchestrator://goals`

High-level goals and roadmap (`goals.md`). Returns raw markdown text.
Returns `"No goals defined."` if the file has not been written yet.

---

### `orchestrator://model-stats`

Per-model performance statistics — win rates, scores by language and role.

**Shape:** JSON object keyed by model name; each value is a stats dict with
fields like `wins`, `losses`, `avg_score`, `by_language`, `by_role`.

---

### `orchestrator://agents`

All configured agent roles with their metadata. Does not include prompt
templates; use the `orchestrator://agents/{role}` template for full detail.

**Shape:** JSON object keyed by role name; each value is the agent's
`to_dict()` output (name, model, language, schema keys, etc.).

---

### `orchestrator://gates`

Current safety gate rules and trigger statistics.

**Shape:** JSON from `gates.get_gates_summary()` — includes active rules,
trigger counts, and severity breakdown.

---

### `orchestrator://gates/lessons`

Recent safety lessons and incident history.

**Shape:** JSON from `gates.get_lessons_summary()` — includes recent
incidents, patterns learned, and timestamps.

---

### `orchestrator://dream/log`

History of dream consolidation cycles with health scores.

**Shape:** JSON array of cycle entries, each with `timestamp`,
`health_score`, `health_rating`, and per-layer stats.

---

### `orchestrator://contract`

Machine-readable MCP contract: version + enumerated tools, resources,
templates, and prompts. Introspects FastMCP managers at call time — cannot
drift from the live registration.

**Shape:**

```json
{
  "version": "1.0.0",
  "name": "AI Orchestrator",
  "tools": [
    { "name": "orchestrate", "description": "..." },
    ...
  ],
  "resources": [
    { "uri": "orchestrator://health", "name": "resource_health", "description": "..." },
    ...
  ],
  "templates": [
    { "uri_template": "orchestrator://agents/{role}", "name": "resource_agent_detail", "description": "..." }
  ],
  "prompts": [
    {
      "name": "plan_task",
      "description": "...",
      "arguments": [
        { "name": "task", "required": true },
        { "name": "target", "required": false }
      ]
    },
    ...
  ]
}
```

---

## Resource Templates

### `orchestrator://agents/{role}`

Full agent configuration including prompt templates for a specific role.

| Path parameter | Description |
|---|---|
| `role` | Agent role name: `planner`, `generator`, `judge`, `optimizer`, `troubleshooter`, `tool_dispatch` |

**Shape:** JSON object with all `to_dict()` fields plus:

```json
{
  "system_prompt": "<full system prompt markdown>",
  "user_prompt": "<full user prompt markdown>",
  "user_prompt_multi": "<multi-turn variant>",
  "schema": { ... },
  "variants": { ... }
}
```

Returns `{"error": "Agent role '<role>' not found"}` for unknown roles.

---

## Prompts

### `plan_task`

Generate a structured implementation plan for a coding task.

| Argument | Type | Required | Description |
|---|---|---|---|
| `task` | `str` | yes | Description of what to build |
| `target` | `str` | no (default: `"pi-1"`) | Target device |

Produces a prompt that asks an LLM to decide language, project type, files,
dependencies, and execution mode for the given task on the given target.

---

### `review_code`

Review code quality and suggest improvements.

| Argument | Type | Required | Description |
|---|---|---|---|
| `code` | `str` | yes | The code to review |
| `task` | `str` | yes | What the code is supposed to do |

Produces a prompt that asks an LLM to score correctness, robustness,
security, performance, structure, and overall quality (each 0–10) and list
specific, actionable improvements.

---

### `troubleshoot_error`

Diagnose and fix a runtime error in generated code.

| Argument | Type | Required | Description |
|---|---|---|---|
| `code` | `str` | yes | The failing code |
| `error` | `str` | yes | The error output |
| `task` | `str` | yes | What the code was supposed to do |

Produces a prompt that asks an LLM to explain what went wrong and provide
corrected code.

---

## Error responses

Tools that detect invalid arguments (e.g., unknown `deploy_target`, missing
`run_id`) return a JSON object with an `error` key. Successful calls return
a domain-specific shape with no `error` key.

## See also

- [RUNBOOK.md](../RUNBOOK.md) — server setup, auth, deployment.
- [ROADMAP.md](../ROADMAP.md) — Phase 1.7 entry covers this contract.
- The Python client SDK at `/opt/ai-orchestrator-client/` does NOT yet
  expose MCP — it speaks REST and `/ws`. MCP is for external AI tooling.
