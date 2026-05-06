# Phase J Scope β — LlmCall Citation-Grade Fidelity

**Status:** DONE 2026-05-06 on branch `feat/scope-beta-llm-call-fidelity`
(3 commits, ~120 LOC, all 7 placeholders dropped).
**Created:** 2026-05-06
**Relates to:** `evidence/builder.py::_record_to_llm_call`, `core/llm_call_log.py`

---

## Resolution Summary (2026-05-06)

All seven Scope α placeholders are now sourced from real runtime data:

| LlmCall field | Captured by |
|---|---|
| `call_id` | `task_run.id` in `prefect_io.state_hooks.on_task_completion` |
| `role` / `target.role` | new `agent_role` kwarg threaded through 12 call sites in `orchestration/__init__.py` + `tools/__init__.py`; coerced to the `LlmTarget` Literal in `_record_to_llm_call` (unknown values → `"generator"`) |
| `target.host` | parsed from `task_run.parameters['url']` via `_host_from_url()` in `evidence/builder.py` |
| `target.model_digest` / `target.model_size_bytes` | `_get_model_metadata()` in `llm/ollama.py` does a cached `/api/show` once per model and stamps the result into the `LlmResponse` envelope under `_orchestrator_*` keys; the state hook lifts them from `state.result().envelope` |
| `response_text` | `_annotate_envelope()` in `llm/ollama.py` writes the response body into the envelope under `_orchestrator_response_text`; state hook reads it |
| `started_at` | `task_run.start_time` in `on_task_completion` (was already accessed for duration_ms) |

Tests added: 6 in `tests/test_ollama_metadata.py`, 1 in
`tests/test_state_hooks.py`, 5 in `tests/test_evidence_llm_call_mapping.py`,
plus 2 schema tests in `tests/test_llm_call_log.py`. Default suite: 117
pass / 3 deselected (was 103 before Scope β).

The legacy fallback path in `_record_to_llm_call` is kept on purpose: a
record from before Scope β (no `agent_role`, no `server_url`, no
`started_at`) still produces a valid `LlmCall` with sane defaults so
mid-flight records during the upgrade don't break bundle build.

---

## Original spec (kept for posterity)

---

## Summary

Phase J Scope α (merged on branch `feat/prefect-integration`) drains
`LLM_CALL_LOG` per run into the evidence bundle's `RunRecord.llm_calls[]`
array. This is a best-effort mapping with the following placeholder values:

| LlmCall field | Scope α value | Why placeholder |
|---|---|---|
| `call_id` | `str(uuid.uuid4())` | No stable call ID generated at task-start yet |
| `role` | `"generator"` (hardcoded) | Runtime doesn't infer from message roles |
| `target.host` | `"ollama-runtime-unknown"` | Not propagated from `llm.ollama` config |
| `target.model_digest` | `"sha256-placeholder-scope-beta"` | Requires `ollama show` at call time |
| `target.model_size_bytes` | `0` | Same — requires `ollama show` |
| `response_text` | `""` | State hook captures token count, not text body |
| `started_at` | `now() - duration_ms` | Prefect task `start_time` not threaded through |

---

## What Scope β Must Capture (at the state-hook level)

### 1. `call_id`
- **Where:** Generate at task-start hook time (Prefect `on_task_start` or via
  `prefect_io/state_hooks.py` before the LLM call).
- **Mechanism:** Pass the UUID through to the LLM call layer so it can be
  included in `LlmCallRecord`. Attach it to the Prefect task_run tags or
  use a `contextvars.ContextVar` thread-local.

### 2. `role`
- **Where:** Infer from `rendered_messages` — the system prompt contains the
  role name, or use the Prefect task name (tasks are named `planner_task`,
  `generator_task`, etc. per Phase 1.3 conventions).
- **Mechanism:** Pass the task name through to `LlmCallRecord` as a new
  `agent_role` field (Scope β adds this field to the dataclass).

### 3. `target.host` and `target.server_url`
- **Where:** `llm/ollama.py` — `resolve_chat_url()` already picks the host.
  Capture the resolved URL at call time and pass it through to the record.
- **Mechanism:** Add `server_url: str` to `LlmCallRecord`; populate it in
  the Ollama client's call path before enqueueing the record.

### 4. `target.model_digest` and `target.model_size_bytes`
- **Where:** `llm/ollama.py` — query `ollama show <model>` once per model per
  session, cache the result. Include digest and size in the record.
- **Mechanism:** Add `model_digest: str` and `model_size_bytes: int` to
  `LlmCallRecord`; populate from the cached `ollama show` response.

### 5. `response_text`
- **Where:** `prefect_io/state_hooks.py` — the `on_task_completion` hook
  currently extracts `response_tokens` from the task result. Extend it to
  also extract the response text body (the full generated string).
- **Mechanism:** Add `response_text: str` to `LlmCallRecord`; populate via
  `state.result()` in the hook (the result dict already contains the text).

### 6. `started_at`
- **Where:** Prefect exposes `task_run.start_time` as a `datetime` inside
  the `on_task_completion` hook via `task_run.start_time`.
- **Mechanism:** Add `started_at: datetime` to `LlmCallRecord`; populate
  from `task_run.start_time` in the hook. Remove the `now() - duration_ms`
  approximation from `_record_to_llm_call`.

---

## Files to Touch in Scope β

1. **`core/llm_call_log.py`** — Add fields to `LlmCallRecord`:
   `agent_role`, `server_url`, `model_digest`, `model_size_bytes`,
   `response_text`, `started_at`.

2. **`llm/ollama.py`** — Cache `ollama show` results; pass `server_url`,
   `model_digest`, `model_size_bytes` through to caller so they can be
   included in the record.

3. **`prefect_io/state_hooks.py`** — In `on_task_completion` (for
   `llm-call`-tagged tasks): extract `response_text` and `started_at`
   from the task run; populate new `LlmCallRecord` fields.

4. **`evidence/builder.py`** — Remove the placeholder fallback in
   `_record_to_llm_call`; use direct field mappings once all fields are
   present in `LlmCallRecord`. Remove the Scope α comments.

---

## Estimated Effort

~3 commits, ~2–3 hours of focused work.

**Suggested commit sequence:**
1. `feat(llm-call-log): add agent_role, server_url, digest, response_text, started_at fields`
2. `feat(prefect-hooks): populate new LlmCallRecord fields in on_task_completion`
3. `feat(evidence): remove Scope α placeholders in _record_to_llm_call (closes Phase J β)`

---

## Notes

- `LlmTarget.role` is a `Literal` type — valid values are `"planner"`,
  `"judge"`, `"generator"`, `"optimizer"`, `"troubleshooter"`,
  `"tool_dispatch"`. Scope β must map agent role strings to this Literal.
- `SamplingParams` has `extra="allow"` so backend-specific keys (e.g.
  `mirostat`) survive round-trips without schema changes.
- The `call_id` must be stable across bundle re-builds if the bundle is
  re-signed. Consider generating it once and persisting it alongside the
  run artifact tree rather than regenerating each time.
