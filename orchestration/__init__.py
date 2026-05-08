"""Orchestration loop — context builders, agent dispatchers, run lifecycle.

Single-file for the initial split (commit 0.g.7). Plan calls for a
`orchestration/loop.py` containing run_orchestration and the agents kept
alongside in agents/, but per-role config split is already done via
agents/<role>/. The Python implementations of the five agent functions
live here for now.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from pydantic import BaseModel
from prefect import flow, task, unmapped
from prefect_io.state_hooks import (
    on_running, on_completion, on_failure, on_cancelled,
)

from agents.loader import load_agent
from core.config import (
    CONFIG, OLLAMA_MAIN_URL, OLLAMA_JUDGE_URL, OLLAMA_PLANNER_URL,
    OLLAMA_MAIN, OLLAMA_JUDGE, OLLAMA_MAIN_CHAT, OLLAMA_JUDGE_CHAT,
    OLLAMA_PLANNER_CHAT, OLLAMA_PLANNER, OLLAMA_EMBED,
    HINDSIGHT_URL, HINDSIGHT_BANK, HINDSIGHT_ENABLED, HINDSIGHT_TIMEOUT,
    NOTIFY_ENABLED, ORCHESTRATOR_URL,
    SSH_TARGETS, SSH_TIMEOUT, DEPLOY_BASE,
    TARGET_SCORE, MAX_ITERATIONS, MAX_TROUBLESHOOT_ATTEMPTS,
    JUDGE_FALLBACK_MODEL,
    SIMILARITY_THRESHOLD, REUSE_SCORE_THRESHOLD,
    TIMEOUT_LLM_GENERATE, TIMEOUT_LLM_STRUCTURED,
    DREAM_AUTO_INTERVAL,
)
from core.paths import (
    PROJECTS_DIR, LOG_DIR, MEMORY_DIR, REFERENCE_DIR,
    SANDBOX_DIR, SANDBOX_VENV,
    RUN_INDEX_FILE,
    PROMPT_INDEX, EMBED_CACHE, NEGATIVE_MEMORY, MODEL_STATS,
)
from core.runtime import (
    RUN_STATUS, ORCHESTRATOR_PAUSED, log,
    _update_run_status, _init_run_status,
    _load_run_index, _persist_run_index,
)
from execution import (
    ssh_command, deploy_file, deploy_project, persistent_deploy,
    sudo_command, validate_target,
    verify_local, verify_remote, verify_code, verify_files,
    VERIFY_CONFIG, PYTHON_STDLIB, LANG_HANDLERS,
    get_lang_handler, get_verifiable_extensions,
    detect_python_deps, detect_node_deps, detect_bash_deps,
    detect_all_dependencies, get_project_modules, sanitize_packages,
    ensure_system_dependencies, SYSTEM_DEPS, SUDO_ALLOWED, SUDO_ENABLED,
    sandbox_execute, sandbox_execute_server,
    ensure_venv, install_dependencies,
    check_port_available, find_available_port, patch_port_in_files,
    environment_inspector, SAFE_PKG_PATTERN,
)
from gates import check_gate, record_runtime_failure
from llm.ollama import (
    query_ollama_api, query_ollama, query_ollama_structured,
    resolve_chat_url, resolve_generate_url,
)
from llm.repair import safe_parse_json
from llm.extract import (
    extract_code, extract_files, format_files_for_prompt,
    LLM_ARTIFACTS, FILE_MARKER,
)
from memory_pkg import (
    load_prompt_index, save_prompt_index,
    load_negative_memory,
    load_model_stats,
    generate_embedding, find_similar, find_negative_matches,
    update_memory, update_negative_memory, update_model_stats,
    get_model_recommendation, build_memory_context,
    load_identity, load_primer, rewrite_primer,
    load_goals, update_goal_status,
    load_session_log, record_session,
    load_target_identity, auto_update_target_identity,
    hindsight_recall, hindsight_retain, hindsight_get_mental_models,
    build_hindsight_retain_content,
    format_hindsight_recall_for_planner, format_mental_models_for_planner,
    vault_after_run,
)
from notifications import notify_run_complete, notify_run_started
from references_pkg import load_reference_content, MAX_REFERENCE_CONTENT_CHARS
from tools import run_tools
from gates import consolidate_lessons
from dream import run_dream
import llm.ollama as _llm_ollama  # for _url_cache access
from manifest import write_run_manifest

# Run counter for auto-dream trigger
_run_counter_since_dream = 0

# SAFE_FILENAME used by run_orchestration for filename safety on file outputs
SAFE_FILENAME = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]+$")


# ── Agent schemas (loaded at import time) ─────────
# Originally lived in app.py via _load_agent_schema().
def _load_agent_schema(role: str, fallback: dict) -> dict:
    """Load schema.json from agents/<role>/, falling back to the embedded default."""
    try:
        cfg = load_agent(role)
        schema = getattr(cfg, "schema", None)
        return schema if schema else fallback
    except Exception:  # noqa: BLE001
        return fallback


PLAN_SCHEMA = _load_agent_schema("planner", {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "entrypoint": {"type": "string"},
        "deps": {"type": "array", "items": {"type": "string"}},
        "approach": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object"}},
        # Phase 3.2 SmartPause — planner self-confidence in [0, 1].
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["language", "entrypoint", "approach"],
})

JUDGE_SCHEMA = _load_agent_schema("judge", {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "reasoning": {"type": "string"},
        "improvements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "reasoning"],
})

TOOL_DISPATCH_SCHEMA = _load_agent_schema("tool_dispatch", {
    "type": "object",
    "properties": {
        "tools_to_run": {"type": "array", "items": {"type": "object"}},
        "reasoning": {"type": "string"},
    },
    "required": ["tools_to_run"],
})

# Replace bare _url_cache references with module-qualified ones at runtime.
# orchestration.refresh_models() calls _llm_ollama._refresh_url_cache() and reads _llm_ollama._url_cache.
_url_cache = _llm_ollama._url_cache  # initial binding; refresh helpers update the dict in-place




# ------------------------------------------------
def get_loaded_models():
    """Check which models are currently loaded in memory on both Ollama servers."""

    loaded = {}

    # main server
    ps = query_ollama_api(OLLAMA_MAIN_URL, "/api/ps")
    if ps and "models" in ps:
        for m in ps["models"]:
            name = m.get("name", "?")
            size_gb = m.get("size", 0) / (1024 ** 3)
            loaded[f"main:{name}"] = {
                "server": "main",
                "model": name,
                "size_gb": round(size_gb, 1),
                "expires": m.get("expires_at", "")
            }

    # judge server (may be same as planner)
    if OLLAMA_JUDGE_URL != OLLAMA_MAIN_URL:
        ps = query_ollama_api(OLLAMA_JUDGE_URL, "/api/ps")
        if ps and "models" in ps:
            for m in ps["models"]:
                name = m.get("name", "?")
                size_gb = m.get("size", 0) / (1024 ** 3)
                loaded[f"judge:{name}"] = {
                    "server": "judge",
                    "model": name,
                    "size_gb": round(size_gb, 1),
                    "expires": m.get("expires_at", "")
                }

    return loaded


def get_available_models():
    """List all models available (downloaded) on both servers."""

    available = {"main": [], "judge": []}

    tags = query_ollama_api(OLLAMA_MAIN_URL, "/api/tags")
    if tags and "models" in tags:
        for m in tags["models"]:
            available["main"].append(m.get("name", "?"))

    if OLLAMA_JUDGE_URL != OLLAMA_MAIN_URL:
        tags = query_ollama_api(OLLAMA_JUDGE_URL, "/api/tags")
        if tags and "models" in tags:
            for m in tags["models"]:
                available["judge"].append(m.get("name", "?"))

    return available


def get_orchestrator_health():
    """Check orchestrator system resources (local)."""

    health = {}

    try:
        import shutil
        disk = shutil.disk_usage("/opt/ai-orchestrator")
        health["disk_free_gb"] = round(disk.free / (1024 ** 3), 1)
        health["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)
        health["disk_used_pct"] = round((disk.used / disk.total) * 100, 1)
    except OSError:
        pass

    # memory from /proc/meminfo (we're on Linux)
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    meminfo[key] = int(val)  # kB

            total = meminfo.get("MemTotal", 0)
            available = meminfo.get("MemAvailable", 0)
            if total > 0:
                health["ram_total_gb"] = round(total / (1024 ** 2), 1)
                health["ram_available_gb"] = round(available / (1024 ** 2), 1)
                health["ram_used_pct"] = round(((total - available) / total) * 100, 1)
    except (OSError, ValueError, KeyError):
        pass

    # load average
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            health["load_1m"] = float(parts[0])
            health["load_5m"] = float(parts[1])
            health["load_15m"] = float(parts[2])
    except (OSError, ValueError, IndexError):
        pass

    return health


def get_active_runs():
    """Get currently running (incomplete) orchestration jobs."""

    active = []
    for rid, info in RUN_STATUS.items():
        if not info.get("completed", True):
            active.append({
                "run_id": rid,
                "project": info.get("project", "?"),
                "phase": info.get("phase", "?"),
                "score": info.get("score", 0)
            })
    return active


def get_recent_completed_runs(n=5):
    """Get the last N completed runs from RUN_STATUS (in-memory)."""

    completed = []
    for rid, info in RUN_STATUS.items():
        if info.get("completed", False) and info.get("result"):
            result = info["result"]
            completed.append({
                "run_id": rid,
                "project": info.get("project", "?"),
                "score": result.get("score", 0),
                "language": result.get("language", "?"),
                "success": result.get("execution", {}).get("returncode", -1) == 0,
                "model": result.get("winning_model", "?"),
                "deployed_to": result.get("deployed_to")
            })

    return completed[-n:]


def get_deployed_project_count(target, run_id):
    """Quick count of deployed projects on a target (avoids full metadata fetch)."""

    resolve = ssh_command(target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()
    if not base:
        return 0

    count_result = ssh_command(
        target,
        f"find {base} -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l"
    )

    try:
        return int(count_result["stdout"].strip())
    except (ValueError, TypeError):
        return 0


def gather_live_context(target, run_id):
    """
    Layer 3: Gather all live system state.
    Called at the start of every orchestration run.
    Returns a dict with all context sections.
    """

    log(run_id, "gathering live context")

    context = {}

    # loaded models (what's hot in GPU/RAM right now)
    context["loaded_models"] = get_loaded_models()

    # available models
    context["available_models"] = get_available_models()

    # orchestrator health
    context["orchestrator_health"] = get_orchestrator_health()

    # active runs (concurrency check)
    context["active_runs"] = get_active_runs()

    # recent completed runs (what just happened)
    context["recent_runs"] = get_recent_completed_runs(5)

    # deployed project count on target
    context["deployed_count"] = get_deployed_project_count(target, run_id)

    # time since last run
    sessions = load_session_log()
    if sessions:
        last_activity = sessions[-1].get("last_activity", "")
        try:
            last_ts = datetime.fromisoformat(last_activity)
            gap = datetime.utcnow() - last_ts
            context["minutes_since_last_run"] = round(gap.total_seconds() / 60.0, 1)
        except (ValueError, TypeError):
            context["minutes_since_last_run"] = None
    else:
        context["minutes_since_last_run"] = None

    # memory file sizes (detect if anything is getting too large)
    for name, path in [("prompt_index", PROMPT_INDEX), ("negative_memory", NEGATIVE_MEMORY),
                       ("model_stats", MODEL_STATS), ("embed_cache", EMBED_CACHE)]:
        try:
            size_kb = path.stat().st_size / 1024
            context[f"{name}_size_kb"] = round(size_kb, 1)
        except OSError:
            pass

    log(run_id, f"live context: {len(context.get('loaded_models', {}))} models loaded, "
                f"{len(context.get('active_runs', []))} active runs, "
                f"{context.get('deployed_count', 0)} deployed projects")

    return context


def format_live_context_for_planner(live_ctx):
    """Format the live context dict into a readable string for the planner."""

    lines = ["LIVE SYSTEM STATUS:"]

    # loaded models
    loaded = live_ctx.get("loaded_models", {})
    if loaded:
        lines.append("  Models currently in memory:")
        for key, info in loaded.items():
            lines.append(f"    {info['server']}: {info['model']} ({info['size_gb']}GB)")
    else:
        lines.append("  No models currently loaded (cold start expected)")

    # orchestrator health
    health = live_ctx.get("orchestrator_health", {})
    if health:
        ram = health.get("ram_available_gb")
        disk = health.get("disk_free_gb")
        load = health.get("load_1m")
        parts = []
        if ram is not None:
            parts.append(f"RAM free: {ram}GB")
        if disk is not None:
            parts.append(f"Disk free: {disk}GB")
        if load is not None:
            parts.append(f"Load: {load}")
        if parts:
            lines.append(f"  Orchestrator: {', '.join(parts)}")

    # active runs
    active = live_ctx.get("active_runs", [])
    if active:
        lines.append(f"  WARNING: {len(active)} other run(s) currently in progress")
        for run in active[:3]:
            lines.append(f"    {run['project']}: {run['phase']}")

    # recent runs
    recent = live_ctx.get("recent_runs", [])
    if recent:
        lines.append("  Recent results (this session):")
        for run in recent[-3:]:
            status = "ok" if run.get("success") else "FAIL"
            lines.append(f"    {run.get('project', '?')}: {run.get('language', '?')} "
                         f"score={run.get('score', 0)} {status} ({run.get('model', '?')})")

    # deployed count
    deployed = live_ctx.get("deployed_count", 0)
    if deployed > 0:
        lines.append(f"  {deployed} project(s) deployed on target")

    # time gap
    gap = live_ctx.get("minutes_since_last_run")
    if gap is not None and gap > 60:
        hours = gap / 60
        lines.append(f"  Note: {hours:.1f} hours since last run — check primer for context")

    return "\n".join(lines)


# ------------------------------------------------
# BRIEFING (full status overview)
# ------------------------------------------------

def build_briefing():
    """
    Build a comprehensive status briefing. Useful for the Web UI
    and for context recovery after development gaps.
    """

    # identity summary (first 3 lines)
    identity = load_identity()
    identity_summary = "\n".join(identity.splitlines()[:5])

    # primer
    primer = load_primer()

    # goals
    goals = load_goals()

    # model stats summary
    stats = load_model_stats()
    model_lines = []
    for model, s in stats.items():
        if s["total_runs"] < 1:
            continue
        avg = s["total_score"] / s["total_runs"]
        wins = s["wins"]
        model_lines.append(f"  {model}: avg {avg:.1f}, {wins} wins, {s['total_runs']} runs")
    model_summary = "\n".join(model_lines) if model_lines else "  No stats yet."

    # recent sessions
    sessions = load_session_log()
    session_lines = []
    for sess in reversed(sessions[-5:]):
        sid = sess.get("session_id", "?")
        count = sess.get("run_count", 0)
        started = sess.get("started", "?")[:16]
        session_lines.append(f"  {sid}: {count} runs, started {started}")
    session_summary = "\n".join(session_lines) if session_lines else "  No sessions recorded."

    # negative memory count
    neg_count = len(load_negative_memory())

    # positive memory count
    pos_count = len(load_prompt_index())

    return {
        "identity_summary": identity_summary,
        "primer": primer,
        "goals": goals,
        "model_performance": model_summary,
        "recent_sessions": session_summary,
        "live_system": {
            "loaded_models": get_loaded_models(),
            "orchestrator_health": get_orchestrator_health(),
            "active_runs": get_active_runs(),
        },
        "memory_counts": {
            "positive": pos_count,
            "negative": neg_count,
            "models_tracked": len(stats)
        }
    }


# ------------------------------------------------
# LAYERED CONTEXT BUILDER (for planner)
# ------------------------------------------------

def build_full_planner_context(prompt, language, project_type, live_ctx=None,
                                run_id="ctx", target=None):
    """
    Assemble all memory layers into a single context string for the planner.
    Order: live status → target identity → primer → goals → semantic memory → hindsight deep memory
    This is the "double-read" — identity appears in the system prompt AND here.
    """

    sections = []

    # Layer 3: live context (system state right now)
    if live_ctx:
        live_str = format_live_context_for_planner(live_ctx)
        if live_str:
            sections.append(live_str)

    # Target-specific identity (hardware, notes, quirks for this node)
    if target:
        target_id = load_target_identity(target)
        if target_id:
            sections.append(f"TARGET NODE IDENTITY ({target}):\n{target_id[:1500]}")

    # Layer 2: primer (current session state)
    primer = load_primer()
    if primer and "No runs recorded yet" not in primer:
        sections.append(f"CURRENT SESSION STATE:\n{primer[:1500]}")

    # Goal memory: active goals and roadmap
    goals = load_goals()
    if goals:
        # extract just the active goals section, not the full roadmap
        goal_lines = []
        in_active = False
        for line in goals.splitlines():
            if "## Active goals" in line:
                in_active = True
                continue
            if in_active and line.startswith("## "):
                break
            if in_active and line.strip():
                goal_lines.append(line)
        if goal_lines:
            sections.append("ACTIVE GOALS:\n" + "\n".join(goal_lines[:15]))

    # Layer 4-lite: semantic memory (positive + negative + model stats)
    memory_context = build_memory_context(prompt, language, project_type)
    if memory_context:
        sections.append(memory_context)

    # Layer 4-deep: Hindsight recall (knowledge graph + entity relationships)
    if HINDSIGHT_ENABLED:
        recall_result = hindsight_recall(prompt, run_id, max_tokens=1500)
        hindsight_str = format_hindsight_recall_for_planner(recall_result)
        if hindsight_str:
            sections.append(hindsight_str)

        # Layer 4-meta: Hindsight mental models (synthesized long-term insights)
        mental_models = hindsight_get_mental_models(run_id)
        mm_str = format_mental_models_for_planner(mental_models)
        if mm_str:
            sections.append(mm_str)

    return "\n\n---\n\n".join(sections) if sections else ""



# ------------------------------------------------
# REQUEST MODEL
# ------------------------------------------------

class OrchestrateRequest(BaseModel):

    project_name: str
    prompt: str

    planner_model: str
    generator_models: list
    judge_model: str

    inspector_model: str | None = None
    optimizer_model: str | None = None
    troubleshooter_model: str | None = None

    max_iterations: int | None = None
    deploy_target: str
    reference_files: list[str] | None = None



@task(name="planner_agent", retries=0)
def planner_agent(prompt, model, env, memory_context, run_id):

    # Load planner agent config from agents/planner/
    planner_cfg = load_agent("planner")

    # Layer 1: Identity as system prompt (first read)
    identity = load_identity()

    system_prompt = planner_cfg.render_system_prompt(identity=identity)

    user_prompt = planner_cfg.render_user_prompt(
        prompt=prompt,
        env=json.dumps(env, indent=2),
        memory_context=memory_context if memory_context else "No past solutions or failures on record.",
    )

    # try structured output first (uses /api/chat with schema enforcement)
    result = query_ollama_structured(
        model,
        system_prompt,
        user_prompt,
        PLAN_SCHEMA,
        resolve_chat_url(model),
        run_id,
        agent_role="planner",
    ).parsed

    if result:

        # validate and fix up the plan
        lang = result.get("language", "python").lower()
        handler = get_lang_handler(lang)
        result["language"] = lang

        if not result.get("entrypoint"):
            result["entrypoint"] = handler["default_entrypoint"]

        if not result.get("files"):
            result["files"] = {result["entrypoint"]: "main script"}

        if result["entrypoint"] not in result["files"]:
            result["files"][result["entrypoint"]] = "main script"

        if not result.get("dependencies"):
            result["dependencies"] = []

        if not result.get("steps"):
            result["steps"] = []

        # normalize project_type and port
        ptype = result.get("project_type", "script").lower()
        if ptype not in ("script", "server"):
            ptype = "script"
        result["project_type"] = ptype

        port = result.get("port", 0)
        if not isinstance(port, int) or port < 0:
            port = 0
        result["port"] = port

        # normalize execution_mode
        emode = result.get("execution_mode", "generate").lower()
        if emode not in ("generate", "tools_only", "tools_then_generate"):
            emode = "generate"
        result["execution_mode"] = emode

        # Phase 3.2 SmartPause — clamp planner self-confidence to [0, 1].
        # Older models that omit it are treated as confidence=1.0 so
        # SmartPause never trips on absence (lying with silence is the
        # default; lying with a number is the planner's choice).
        try:
            conf = float(result.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        result["confidence"] = max(0.0, min(1.0, conf))

        log(run_id, f"planner: {lang} {ptype}, {len(result['files'])} file(s), entrypoint={result['entrypoint']}, mode={emode}, confidence={result['confidence']:.2f}" +
            (f", port={port}" if ptype == "server" else ""))
        return result

    # fallback: use /api/generate without schema enforcement
    log(run_id, "structured planner failed, falling back to unstructured")

    raw = query_ollama(model, user_prompt, OLLAMA_PLANNER, run_id, agent_role="planner").text

    parsed = safe_parse_json(raw, run_id, context="planner fallback")

    if parsed and isinstance(parsed, dict):
        lang = parsed.get("language", "python").lower()
        handler = get_lang_handler(lang)
        parsed["language"] = lang
        if not parsed.get("entrypoint"):
            parsed["entrypoint"] = handler["default_entrypoint"]
        if not parsed.get("files"):
            parsed["files"] = {parsed["entrypoint"]: "main script"}
        ptype = parsed.get("project_type", "script").lower()
        if ptype not in ("script", "server"):
            ptype = "script"
        parsed["project_type"] = ptype
        parsed["port"] = parsed.get("port", 0)
        emode = parsed.get("execution_mode", "generate").lower()
        if emode not in ("generate", "tools_only", "tools_then_generate"):
            emode = "generate"
        parsed["execution_mode"] = emode
        # Phase 3.2 SmartPause — same clamp as the structured path.
        try:
            conf = float(parsed.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        parsed["confidence"] = max(0.0, min(1.0, conf))
        return parsed

    log(run_id, "planner fallback also failed, using defaults")
    return {
        "language": "python",
        "project_type": "script",
        "execution_mode": "generate",
        "port": 0,
        "entrypoint": "main.py",
        "files": {"main.py": "script"},
        "dependencies": [],
        "steps": [],
        # Phase 3.2 SmartPause: defaults imply low confidence; the
        # structured planner failed AND the unstructured fallback failed
        # to parse, so we should not silently mark this run "high
        # confidence" — better that SmartPause trip and the operator
        # decide.
        "confidence": 0.0,
    }


# ------------------------------------------------
# JUDGE (structured output)
# ------------------------------------------------

@task(name="judge_score", retries=0)
def judge_score(files, prompt, plan, model, run_id):

    file_list = ", ".join(files.keys())
    formatted = format_files_for_prompt(files)
    entrypoint = plan.get("entrypoint", "main.py")

    # Load judge agent config from agents/judge/
    judge_cfg = load_agent("judge")

    system_prompt = judge_cfg.render_system_prompt()

    user_prompt = judge_cfg.render_user_prompt(
        prompt=prompt,
        file_list=file_list,
        formatted_files=formatted,
        entrypoint=entrypoint,
    )

    # circuit breaker: skip primary judge if it already failed this run
    primary_down = RUN_STATUS.get(run_id, {}).get("_judge_primary_down", False)

    if not primary_down:
        # try structured output
        result = query_ollama_structured(
            model,
            system_prompt,
            user_prompt,
            JUDGE_SCHEMA,
            resolve_chat_url(model),
            run_id,
            agent_role="judge",
        ).parsed

        if result and isinstance(result, dict):
            overall = result.get("overall", 0)
            # clamp to 0-10
            if isinstance(overall, (int, float)):
                overall = max(0, min(10, overall))
            else:
                overall = 0
            log(run_id, f"judge score: {overall}")
            return overall, result

        # fallback: unstructured
        log(run_id, "structured judge failed, falling back to unstructured")

        raw = query_ollama(model, user_prompt, OLLAMA_JUDGE, run_id, agent_role="judge").text

        parsed = safe_parse_json(raw, run_id, context="judge fallback")

        if parsed and isinstance(parsed, dict):
            overall = parsed.get("overall", 0)
            if isinstance(overall, (int, float)):
                overall = max(0, min(10, overall))
            else:
                overall = 0
            return overall, parsed

        log(run_id, "judge fallback also failed, scoring 0")
        # mark primary judge as down for the remainder of this run
        _update_run_status(run_id, _judge_primary_down=True)
    else:
        log(run_id, "primary judge down, using fallback directly")

    # last resort: try fallback judge on main box if configured
    if JUDGE_FALLBACK_MODEL:
        log(run_id, f"trying fallback judge -> {JUDGE_FALLBACK_MODEL}")

        result = query_ollama_structured(
            JUDGE_FALLBACK_MODEL,
            system_prompt,
            user_prompt,
            JUDGE_SCHEMA,
            OLLAMA_MAIN_CHAT,
            run_id,
            agent_role="judge",
        ).parsed

        if result and isinstance(result, dict):
            overall = result.get("overall", 0)
            if isinstance(overall, (int, float)):
                overall = max(0, min(10, overall))
            else:
                overall = 0
            log(run_id, f"fallback judge score: {overall}")
            return overall, result

        raw = query_ollama(JUDGE_FALLBACK_MODEL, user_prompt, resolve_generate_url(JUDGE_FALLBACK_MODEL), run_id, agent_role="judge").text
        parsed = safe_parse_json(raw, run_id, context="fallback judge")
        if parsed and isinstance(parsed, dict):
            overall = parsed.get("overall", 0)
            if isinstance(overall, (int, float)):
                overall = max(0, min(10, overall))
            else:
                overall = 0
            return overall, parsed

        log(run_id, "fallback judge also failed, scoring 0")

    return 0, {}


# ------------------------------------------------
# GENERATOR (free-form code output)
# ------------------------------------------------

@task(name="generate_candidate", retries=0)
def generate_candidate(model, prompt, plan, env, judge_model, target, run_id, tool_context=""):

    log(run_id, f"generator start -> {model}")

    language = plan.get("language", "python").lower()
    handler = get_lang_handler(language)
    file_descriptions = json.dumps(plan.get("files", {}), indent=2)
    entrypoint = plan.get("entrypoint", handler["default_entrypoint"])
    run_command = handler["run_command"]
    num_files = len(plan.get("files", {entrypoint: "script"}))

    # Load generator agent config from agents/generator/
    gen_cfg = load_agent("generator")

    # language-specific overrides from variants/
    variant = gen_cfg.get_variant(language)

    # default hints if variant doesn't define them
    default_hints = {
        "python": f"Python: {env.get('python', 'python3')}",
        "bash": "Shell: bash",
        "javascript": f"Node.js: {env.get('node', 'node')}",
    }

    lang_hint_tpl = variant.get("lang_hint", default_hints.get(language, f"Language: {language}"))
    lang_hint = gen_cfg.render(lang_hint_tpl, {
        "python_version": env.get("python", "python3"),
        "node_version": env.get("node", "node"),
    })

    extra_rules = variant.get("extra_rules", "")

    tool_section = f"\n{tool_context}\n" if tool_context else ""

    multi = num_files > 1
    gen_prompt = gen_cfg.render_user_prompt(
        multi=multi,
        language=language,
        os=env.get("os", "Linux"),
        lang_hint=lang_hint,
        arch=env.get("arch", "unknown"),
        prompt=prompt,
        tool_section=tool_section,
        extra_rules=extra_rules,
        run_command=run_command,
        entrypoint=entrypoint,
        file_descriptions=file_descriptions,
    )

    raw = query_ollama(model, gen_prompt, resolve_generate_url(model), run_id, agent_role="generator").text

    files = extract_files(raw, plan)

    if not files:
        return {
            "model": model,
            "files": {},
            "score": 0,
            "judge": {}
        }

    all_passed, errors = verify_files(files, language, target, run_id)

    if not all_passed:
        return {
            "model": model,
            "files": files,
            "score": 0,
            "judge": {"verification_errors": errors}
        }

    score, judge = judge_score.submit(files, prompt, plan, judge_model, run_id).result()

    return {
        "model": model,
        "files": files,
        "score": score,
        "judge": judge
    }


# ------------------------------------------------
# OPTIMIZER
# ------------------------------------------------

@task(name="optimizer_agent", retries=0)
def optimizer_agent(files, prompt, judge, plan, model, run_id):

    if not model:
        return files

    log(run_id, "optimizer improving code")

    # Load optimizer agent config from agents/optimizer/
    opt_cfg = load_agent("optimizer")

    language = plan.get("language", "python").lower()
    improve = json.dumps(judge.get("improvements", []), indent=2)
    formatted = format_files_for_prompt(files)
    entrypoint = plan.get("entrypoint", "main.py")
    num_files = len(files)

    if num_files == 1:
        filename = list(files.keys())[0]
        p = opt_cfg.render_user_prompt(
            language=language,
            prompt=prompt,
            improvements=improve,
            code=files[filename],
        )
        raw = query_ollama(model, p, resolve_generate_url(model), run_id, agent_role="optimizer").text
        code = extract_code(raw)
        if code:
            return {filename: code}
        return files

    else:
        p = opt_cfg.render_user_prompt(
            multi=True,
            language=language,
            prompt=prompt,
            improvements=improve,
            formatted_files=formatted,
            entrypoint=entrypoint,
        )
        raw = query_ollama(model, p, resolve_generate_url(model), run_id, agent_role="optimizer").text
        result = extract_files(raw, plan)
        return result if result else files


# ------------------------------------------------
# TROUBLESHOOTER
# ------------------------------------------------

@task(name="troubleshoot", retries=0)
def troubleshoot(files, error, prompt, plan, model, run_id):

    if not model:
        return files

    log(run_id, "troubleshooter fixing runtime error")

    # Load troubleshooter agent config from agents/troubleshooter/
    ts_cfg = load_agent("troubleshooter")

    language = plan.get("language", "python").lower()
    formatted = format_files_for_prompt(files)
    entrypoint = plan.get("entrypoint", "main.py")
    num_files = len(files)

    if num_files == 1:
        filename = list(files.keys())[0]
        p = ts_cfg.render_user_prompt(
            language=language,
            prompt=prompt,
            error=error,
            code=files[filename],
        )
        raw = query_ollama(model, p, resolve_generate_url(model), run_id, agent_role="troubleshooter").text
        code = extract_code(raw)
        if code:
            return {filename: code}
        return files

    else:
        p = ts_cfg.render_user_prompt(
            multi=True,
            language=language,
            prompt=prompt,
            entrypoint=entrypoint,
            error=error,
            formatted_files=formatted,
        )
        raw = query_ollama(model, p, resolve_generate_url(model), run_id, agent_role="troubleshooter").text
        result = extract_files(raw, plan)
        return result if result else files


# ------------------------------------------------
# ORCHESTRATOR CORE
# ------------------------------------------------

@flow(
    name="orchestrate",
    retries=1,
    retry_delay_seconds=60,
    on_running=[on_running],
    on_completion=[on_completion],
    on_failure=[on_failure],
    on_cancellation=[on_cancelled],
)
def run_orchestration(req: OrchestrateRequest, run_id: str):

    try:

        log(run_id, "run started")

        # send start notification
        try:
            notify_run_started(run_id, req.project_name, req.deploy_target, req.prompt)
        except (requests.exceptions.RequestException, OSError) as e:
            log(run_id, f"start notification failed (non-fatal): {e}")
        _run_start_time = time.time()

        env = environment_inspector(req.deploy_target, run_id)

        # auto-profile target identity on first run
        try:
            auto_update_target_identity(req.deploy_target, env, run_id)
        except Exception as e:
            log(run_id, f"target identity auto-update failed (non-fatal): {e}")

        # Layer 3: gather live system state
        live_ctx = gather_live_context(req.deploy_target, run_id)

        # build comprehensive layered memory context
        # (live status + primer + goals + semantic memory + model stats)
        memory_context = build_full_planner_context(req.prompt, "python", "script", live_ctx, run_id, target=req.deploy_target)

        if memory_context:
            log(run_id, "memory context loaded")

        # append reference document summaries to planner context
        if req.reference_files:
            ref_names = ", ".join(req.reference_files)
            memory_context += f"\n\nATTACHED REFERENCE DOCUMENTS: {ref_names}\nThe generator will receive the full content of these documents. Plan accordingly — the user has provided these as context for the task."

        plan = planner_agent.submit(req.prompt, req.planner_model, env, memory_context, run_id).result()

        language = plan.get("language", "python").lower()
        project_type = plan.get("project_type", "script")
        entrypoint = plan.get("entrypoint", get_lang_handler(language)["default_entrypoint"])

        execution_mode = plan.get("execution_mode", "generate")

        # load reference documents if any were attached
        ref_context = ""
        if req.reference_files:
            ref_parts = []
            total_chars = 0
            for ref_name in req.reference_files:
                try:
                    content = load_reference_content(ref_name)
                except Exception as e:
                    log(run_id, f"reference load failed: {ref_name}: {e}")
                    continue
                if content:
                    # truncate individual refs if needed
                    if len(content) > MAX_REFERENCE_CONTENT_CHARS:
                        content = content[:MAX_REFERENCE_CONTENT_CHARS] + f"\n\n[... truncated at {MAX_REFERENCE_CONTENT_CHARS} chars ...]"
                        log(run_id, f"reference truncated: {ref_name}")
                    # check cumulative budget
                    if total_chars + len(content) > MAX_REFERENCE_CONTENT_CHARS * 2:
                        log(run_id, f"reference budget exceeded, skipping remaining refs from {ref_name}")
                        break
                    ref_parts.append(f"--- {ref_name} ---\n{content}")
                    total_chars += len(content)
                    log(run_id, f"reference loaded: {ref_name} ({len(content)} chars)")
                else:
                    log(run_id, f"reference not found or empty: {ref_name}")
            if ref_parts:
                ref_context = "REFERENCE DOCUMENTS:\n" + "\n\n".join(ref_parts) + "\n"

        # tool phase: only runs when planner requested tools
        tool_context = ""
        if execution_mode in ("tools_only", "tools_then_generate"):
            try:
                tool_context = run_tools(req.prompt, plan, env, req.deploy_target, req.planner_model, run_id)
            except Exception as e:
                log(run_id, f"tool phase failed (non-fatal): {e}")
                tool_context = f"TOOL PHASE ERROR: {e}\nThe environment preparation requested by the planner could not be completed. Adapt your code accordingly."

        # tools_only: task fully handled by tools, skip generation entirely
        if execution_mode == "tools_only":
            log(run_id, "execution mode: tools_only — skipping generation")

            summary = tool_context if tool_context else "Tools ran but produced no output."

            # score based on whether tools actually succeeded
            tool_has_errors = not tool_context or "[error]" in tool_context.lower()
            tool_score = 0 if tool_has_errors else 10
            tool_success = not tool_has_errors

            if tool_has_errors:
                log(run_id, "tools_only: tool phase had errors, scoring 0")

            _update_run_status(run_id, completed=True, score=tool_score, stdout=summary, exit_code=0 if tool_success else 1)

            run_dir = Path(PROJECTS_DIR) / req.project_name / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "prompt.txt").write_text(req.prompt)
            (run_dir / "plan.json").write_text(json.dumps(plan, indent=2))
            (run_dir / "tool_output.txt").write_text(summary)

            log(run_id, "run completed")
            _run_elapsed = int(time.time() - _run_start_time)
            try:
                notify_run_complete(
                    run_id=run_id, project_name=req.project_name, score=tool_score,
                    success=tool_success, language=language, deploy_path=None,
                    target=req.deploy_target, winning_model="tools",
                    troubleshoot_attempts=0, elapsed_seconds=_run_elapsed
                )
            except (requests.exceptions.RequestException, OSError) as e:
                log(run_id, f"completion notification failed (non-fatal): {e}")
            return

        iterations = req.max_iterations or MAX_ITERATIONS

        best_files = None
        best_score = 0
        best_judge = {}
        best_model = ""
        all_candidate_results = []  # for model stats tracking

        score_at_iter_start = 0

        for i in range(iterations):

            score_at_iter_start = best_score
            log(run_id, f"generation iteration {i+1}")

            # combine tool context and reference documents for generator
            combined_context = tool_context
            if ref_context:
                combined_context = (combined_context + "\n" + ref_context) if combined_context else ref_context

            # Prefect .map() distributes one task run per generator model.
            # plan and env are dicts; wrap with unmapped() so they're broadcast
            # as scalars instead of iterated over their keys.
            futures = generate_candidate.map(
                model=req.generator_models,
                prompt=req.prompt,
                plan=unmapped(plan),
                env=unmapped(env),
                judge_model=req.judge_model,
                target=req.deploy_target,
                run_id=run_id,
                tool_context=combined_context,
            )

            candidates = []
            for fut, model in zip(futures, req.generator_models):
                try:
                    candidates.append(fut.result())
                except Exception as e:
                    log(run_id, f"generator failed for {model}: {e}")

            candidates.sort(key=lambda x: x["score"], reverse=True)

            if not candidates:
                log(run_id, "all generators failed this iteration")
                continue

            # track all candidate results for model stats
            all_candidate_results.extend(candidates)

            iter_best = candidates[0]
            iter_score = iter_best["score"]

            # only promote this iteration if it beat the cross-iteration best
            if best_files is None or iter_score > best_score:
                best_files = iter_best["files"]
                best_score = iter_score
                best_judge = iter_best.get("judge", {})
                best_model = iter_best.get("model", "")

            _update_run_status(run_id, score=best_score)

            log(run_id, f"best score {best_score}")

            if best_score >= TARGET_SCORE:
                break

            # skip optimizer if judge gave no feedback (connection failure → score 0 with empty judge)
            if not best_judge:
                log(run_id, "no judge feedback available, skipping optimizer")
            else:
                optimized_files = optimizer_agent.submit(
                    best_files,
                    req.prompt,
                    best_judge,
                    plan,
                    req.optimizer_model,
                    run_id
                ).result()

                if optimized_files and optimized_files != best_files:

                    all_passed, _ = verify_files(optimized_files, language, req.deploy_target, run_id)

                    if all_passed:

                        opt_score, opt_judge = judge_score.submit(
                            optimized_files,
                            req.prompt,
                            plan,
                            req.judge_model,
                            run_id
                        ).result()

                        log(run_id, f"optimizer score {opt_score} (was {best_score})")

                        if opt_score > best_score:
                            best_files = optimized_files
                            best_score = opt_score
                            best_judge = opt_judge
                            _update_run_status(run_id, score=best_score)
                        else:
                            log(run_id, "optimizer made it worse, keeping original")

                    else:
                        log(run_id, "optimized code failed verification, keeping original")

            if best_score >= TARGET_SCORE:
                break

            # stagnation exit: if this iteration (generators + optimizer) produced no improvement, stop
            if i > 0 and best_score <= score_at_iter_start:
                log(run_id, "score stagnant, stopping early")
                break

        if not best_files:
            log(run_id, "no valid code was generated across all iterations")

            # record negative memory for total generation failure
            emb = generate_embedding(req.prompt)
            models_tried = list(set(c.get("model", "") for c in all_candidate_results if c.get("model")))
            if not models_tried:
                models_tried = req.generator_models

            update_negative_memory.submit(
                prompt=req.prompt,
                embedding=emb,
                run_id=run_id,
                project=req.project_name,
                language=language,
                error_summary="All generators failed to produce valid code across all iterations",
                failure_stage="generation",
                models_tried=models_tried,
                project_type=project_type
            ).result(raise_on_failure=False)

            _update_run_status(run_id, completed=True, error="All generators failed to produce valid code")

            # notify on total generation failure
            _run_elapsed = int(time.time() - _run_start_time)
            try:
                notify_run_complete(
                    run_id=run_id, project_name=req.project_name, score=0,
                    success=False, language=language, deploy_path=None,
                    target=req.deploy_target, winning_model="none",
                    troubleshoot_attempts=0, elapsed_seconds=_run_elapsed
                )
            except (requests.exceptions.RequestException, OSError) as e:
                log(run_id, f"failure notification failed (non-fatal): {e}")

            return

        log(run_id, "executing sandbox")

        execution = sandbox_execute(req.deploy_target, best_files, entrypoint, language, plan, run_id)

        troubleshoot_attempt = 0

        while execution["returncode"] != 0 and troubleshoot_attempt < MAX_TROUBLESHOOT_ATTEMPTS:

            troubleshoot_attempt += 1

            log(run_id, f"troubleshoot attempt {troubleshoot_attempt}/{MAX_TROUBLESHOOT_ATTEMPTS}")

            fixed_files = troubleshoot.submit(
                best_files,
                execution["stderr"],
                req.prompt,
                plan,
                req.troubleshooter_model,
                run_id
            ).result()

            if not fixed_files or fixed_files == best_files:
                log(run_id, "troubleshooter returned unchanged code, stopping")
                break

            all_passed, _ = verify_files(fixed_files, language, req.deploy_target, run_id)

            if not all_passed:
                log(run_id, "troubleshooter fix failed verification, retrying")
                best_files = fixed_files
                continue

            best_files = fixed_files

            execution = sandbox_execute(req.deploy_target, best_files, entrypoint, language, plan, run_id)

        if execution["returncode"] != 0:
            log(run_id, f"execution still failing after {troubleshoot_attempt} troubleshoot attempts")

        # persistent deploy on success
        deploy_path = None
        if execution["returncode"] == 0:
            try:
                project_modules = get_project_modules(best_files)
                all_deps = detect_all_dependencies(best_files, language, project_modules)

                deploy_path = persistent_deploy(
                    target=req.deploy_target,
                    project_name=req.project_name,
                    files=best_files,
                    entrypoint=entrypoint,
                    language=language,
                    plan=plan,
                    deps=all_deps,
                    run_id=run_id,
                    score=best_score,
                    prompt=req.prompt,
                    run_id_str=run_id
                )
            except Exception as e:
                log(run_id, f"persistent deploy failed (sandbox result still valid): {e}")

        # save artifacts
        run_dir = Path(PROJECTS_DIR) / req.project_name / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        (run_dir / "prompt.txt").write_text(req.prompt)
        (run_dir / "plan.json").write_text(json.dumps(plan, indent=2))
        (run_dir / "environment.json").write_text(json.dumps(env, indent=2))
        (run_dir / "judge.json").write_text(json.dumps(best_judge, indent=2))
        (run_dir / "execution.json").write_text(json.dumps(execution, indent=2))
        (run_dir / "score.txt").write_text(str(best_score))

        src_dir = run_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)

        for filename, content in best_files.items():
            file_path = src_dir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

        (run_dir / "files.json").write_text(json.dumps(best_files, indent=2))

        emb = generate_embedding(req.prompt)

        # record model performance stats for all candidates
        for cand in all_candidate_results:
            cand_model = cand.get("model", "")
            cand_score = cand.get("score", 0)
            was_winner = (cand_model == best_model)
            update_model_stats(
                model=cand_model,
                role="generator",
                language=language,
                score=cand_score,
                was_winner=was_winner,
                succeeded=execution["returncode"] == 0 if was_winner else cand_score > 0,
                project_type=project_type
            )

        if execution["returncode"] == 0:
            # positive memory
            update_memory.submit(
                prompt=req.prompt,
                embedding=emb,
                run_id=run_id,
                score=best_score,
                project=req.project_name,
                language=language,
                success=True,
                winning_model=best_model,
                troubleshoot_attempts=troubleshoot_attempt,
                project_type=project_type
            ).result(raise_on_failure=False)
        else:
            # still record in positive memory (with success=False) for similarity matching
            update_memory.submit(
                prompt=req.prompt,
                embedding=emb,
                run_id=run_id,
                score=best_score,
                project=req.project_name,
                language=language,
                success=False,
                winning_model=best_model,
                troubleshoot_attempts=troubleshoot_attempt,
                project_type=project_type
            ).result(raise_on_failure=False)

            # negative memory — record what went wrong
            error_summary = execution.get("stderr", "")[:500]
            if not error_summary:
                error_summary = f"Exit code {execution.get('returncode', '?')}"

            failure_stage = "execution"
            if troubleshoot_attempt >= MAX_TROUBLESHOOT_ATTEMPTS:
                failure_stage = "troubleshoot_exhausted"

            models_tried = list(set(c.get("model", "") for c in all_candidate_results if c.get("model")))

            update_negative_memory.submit(
                prompt=req.prompt,
                embedding=emb,
                run_id=run_id,
                project=req.project_name,
                language=language,
                error_summary=error_summary,
                failure_stage=failure_stage,
                models_tried=models_tried,
                project_type=project_type
            ).result(raise_on_failure=False)

        # rewrite primer.md with current state (Layer 2)
        env_summary = f"Target: {req.deploy_target}, Python: {env.get('python', '?')}, Node: {env.get('node', '?')}"
        try:
            rewrite_primer.submit(
                run_id=run_id,
                project_name=req.project_name,
                language=language,
                project_type=project_type,
                score=best_score,
                entrypoint=entrypoint,
                files=best_files,
                execution=execution,
                deploy_path=deploy_path,
                prompt=req.prompt,
                plan=plan,
                troubleshoot_attempts=troubleshoot_attempt,
                winning_model=best_model,
                env_summary=env_summary
            ).result(raise_on_failure=False)
        except Exception as e:
            log(run_id, f"primer rewrite failed (non-fatal): {e}")

        # record session (groups runs within time windows)
        try:
            session_id = record_session.submit(
                run_id=run_id,
                project_name=req.project_name,
                prompt=req.prompt,
                language=language,
                score=best_score,
                success=execution["returncode"] == 0,
                winning_model=best_model,
                troubleshoot_attempts=troubleshoot_attempt
            ).result(raise_on_failure=False)
            log(run_id, f"session: {session_id}")
        except Exception as e:
            log(run_id, f"session recording failed (non-fatal): {e}")

        # Layer 4: retain run narrative in Hindsight
        if HINDSIGHT_ENABLED:
            try:
                retain_content = build_hindsight_retain_content(
                    run_id=run_id,
                    project_name=req.project_name,
                    prompt=req.prompt,
                    language=language,
                    project_type=project_type,
                    score=best_score,
                    success=execution["returncode"] == 0,
                    winning_model=best_model,
                    troubleshoot_attempts=troubleshoot_attempt,
                    entrypoint=entrypoint,
                    files=best_files,
                    execution=execution,
                    deploy_path=deploy_path,
                    target=req.deploy_target,
                    plan=plan
                )
                hindsight_retain.submit(retain_content, run_id).result(raise_on_failure=False)
            except Exception as e:
                log(run_id, f"hindsight retain failed (non-fatal): {e}")

        log(run_id, "run completed")

        # send push notification
        _run_elapsed = int(time.time() - _run_start_time)
        try:
            notify_run_complete(
                run_id=run_id,
                project_name=req.project_name,
                score=best_score,
                success=execution["returncode"] == 0,
                language=language,
                deploy_path=deploy_path,
                target=req.deploy_target,
                winning_model=best_model,
                troubleshoot_attempts=troubleshoot_attempt,
                elapsed_seconds=_run_elapsed
            )
        except Exception as e:
            log(run_id, f"notification failed (non-fatal): {e}")

        # write vault notes (L5)
        _run_elapsed_vault = _run_elapsed if '_run_elapsed' in dir() else int(time.time() - _run_start_time)
        try:
            vault_after_run.submit(
                run_id=run_id, project_name=req.project_name, prompt=req.prompt,
                language=language, project_type=project_type, score=best_score,
                success=execution["returncode"] == 0, winning_model=best_model,
                troubleshoot_attempts=troubleshoot_attempt,
                entrypoint=entrypoint, files=best_files, execution=execution,
                deploy_path=deploy_path, target=req.deploy_target, plan=plan,
                best_judge=best_judge, elapsed_seconds=_run_elapsed_vault
            ).result(raise_on_failure=False)
        except Exception as e:
            log(run_id, f"vault write failed (non-fatal): {e}")

        # Gates: consolidate lessons at end of run
        try:
            gate_report = consolidate_lessons.submit(log_fn=log).result(raise_on_failure=False)
            if gate_report.get("promoted_count", 0) > 0:
                log(run_id, f"gates: {gate_report['promoted_count']} new rules auto-promoted")
        except Exception as e:
            log(run_id, f"gates consolidation failed (non-fatal): {e}")

        # Dream: auto-trigger memory consolidation every N runs
        global _run_counter_since_dream
        _run_counter_since_dream += 1
        if DREAM_AUTO_INTERVAL > 0 and _run_counter_since_dream >= DREAM_AUTO_INTERVAL:
            try:
                log(run_id, "dream auto-trigger: consolidating memory")
                # Get available models for pruning
                available = set(_llm_ollama._url_cache.keys()) if _llm_ollama._url_cache else None
                dream_report = run_dream(available_models=available, log_fn=log)
                health = dream_report.get("health", {})
                log(run_id, f"dream: health={health.get('score', '?')}/100 ({health.get('rating', '?')})")
                _run_counter_since_dream = 0
            except Exception as e:
                log(run_id, f"dream auto-trigger failed (non-fatal): {e}")

        # Write per-run SHA256 manifest (Phase 1.5 Phase B).
        # Must run after all artifact writes and before terminal status update.
        try:
            write_run_manifest(run_dir, run_id=run_id)
            _update_run_status(run_id, manifest_status="ok")
            log(run_id, "manifest: SHA256 manifest written")
        except Exception as exc:
            log(run_id, f"manifest write failed (non-fatal): {exc}")
            _update_run_status(run_id, manifest_status="skipped")

        _update_run_status(run_id, completed=True, score=best_score, result={
            "run_id": run_id,
            "score": best_score,
            "language": language,
            "project_type": project_type,
            "winning_model": best_model,
            "troubleshoot_attempts": troubleshoot_attempt,
            "files": list(best_files.keys()),
            "entrypoint": entrypoint,
            "execution": execution,
            "deployed_to": deploy_path
        })

    except Exception as e:

        log(run_id, f"orchestration crashed: {e}")
        _update_run_status(run_id, completed=True, error=str(e))


# ------------------------------------------------
# API ENDPOINTS
# ------------------------------------------------

