import uuid
import subprocess
import requests
import json
import re
import os
import ast
import asyncio
import math
import fcntl
import time
import threading
import shlex
import tempfile
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── core/: paths, config, locks, runtime state ────
from core.paths import (
    CONFIG_PATH, PROJECTS_DIR, LOG_DIR, MEMORY_DIR, REFERENCE_DIR,
    SANDBOX_DIR, SANDBOX_VENV,
    RUN_INDEX_FILE, PROMPT_INDEX, EMBED_CACHE, NEGATIVE_MEMORY, MODEL_STATS,
    SESSION_LOG, IDENTITY_FILE, PRIMER_FILE, GOALS_FILE, TARGET_IDENTITY_DIR,
    TOOL_REGISTRY_PATH,
)
from core.config import (
    CONFIG,
    OLLAMA_MAIN_URL, OLLAMA_JUDGE_URL, OLLAMA_PLANNER_URL,
    OLLAMA_MAIN, OLLAMA_JUDGE, OLLAMA_MAIN_CHAT, OLLAMA_JUDGE_CHAT,
    OLLAMA_PLANNER_CHAT, OLLAMA_PLANNER, OLLAMA_EMBED,
    HINDSIGHT_URL, HINDSIGHT_BANK, HINDSIGHT_ENABLED, HINDSIGHT_TIMEOUT,
    NOTIFY_CONFIG, NOTIFY_ENABLED, NOTIFY_SERVICE,
    NTFY_URL, NTFY_TOPIC, NTFY_PRIORITY, NTFY_URLS,
    GOTIFY_URL, GOTIFY_TOKEN, GOTIFY_PRIORITY, GOTIFY_URLS,
    NOTIFY_ON_SUCCESS, NOTIFY_ON_FAILURE,
    ORCHESTRATOR_URL, NOTIFY_STRATEGY,
    VAULT_CONFIG, VAULT_ENABLED, VAULT_LOCAL_DIR,
    VAULT_REMOTE_HOST, VAULT_REMOTE_USER, VAULT_REMOTE_KEY, VAULT_REMOTE_DIR,
    VAULT_SYNC_ENABLED, VAULT_NAS_ENABLED, VAULT_NAS_PATH,
    SSH_TARGETS, SSH_TIMEOUT, DEPLOY_BASE,
    TARGET_SCORE, MAX_ITERATIONS, MAX_TROUBLESHOOT_ATTEMPTS,
    JUDGE_FALLBACK_MODEL,
    SIMILARITY_THRESHOLD, REUSE_SCORE_THRESHOLD,
    MAX_PROMPT_INDEX_ENTRIES, MAX_EMBED_CACHE_ENTRIES,
    TIMEOUT_EMBEDDING, TIMEOUT_LLM_GENERATE, TIMEOUT_LLM_STRUCTURED,
    TIMEOUT_HINDSIGHT_RETAIN, TIMEOUT_HINDSIGHT_RECALL, TIMEOUT_HINDSIGHT_REFLECT,
    TIMEOUT_VAULT_SYNC, TIMEOUT_VAULT_NAS_SYNC,
    DREAM_AUTO_INTERVAL,
)
from core.locks import locked_read_json, locked_write_json
from core.runtime import (
    RUN_STATUS, ORCHESTRATOR_PAUSED,
    _ws_clients, _ws_lock, _MAIN_LOOP, set_main_loop,
    _ws_broadcast, log,
    _update_run_status, _init_run_status,
    _load_run_index, _persist_run_index,
)

from agents.loader import load_agent, load_all_agents, reload_all as reload_agents, list_roles as list_agent_roles
from dream import run_dream, DREAM_LOG, _load_json as dream_load_json
from gates import (
    init_gates, check_gate, record_lesson, record_runtime_failure,
    consolidate_lessons, load_gates, save_gates, add_gate, remove_gate,
    toggle_gate, get_gates_summary, get_lessons_summary, load_all_lessons,
)

app = FastAPI()

# CORS — allow the graph UI and external tools to access the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# MCP server: mount with proper lifecycle management
from mcp_server import mcp as mcp_instance
import contextlib

@contextlib.asynccontextmanager
async def _lifespan(app_instance):
    # Capture the running event loop so background threads can post coroutines
    # back via asyncio.run_coroutine_threadsafe (used by _ws_broadcast).
    set_main_loop(asyncio.get_running_loop())
    # Start MCP session manager (required for streamable HTTP)
    async with mcp_instance.session_manager.run():
        yield

# Attach lifespan to FastAPI app
app.router.lifespan_context = _lifespan

# Mount MCP Starlette sub-app (route handler only, lifespan managed above)
_mcp_starlette = mcp_instance.streamable_http_app()
# Remove the sub-app's own lifespan to avoid double-init
_mcp_starlette.lifespan_handler = None
app.mount("/mcp", _mcp_starlette)

from llm.ollama import (  # noqa: E402
    query_ollama_api, query_ollama, query_ollama_structured,
    resolve_chat_url, resolve_generate_url, _refresh_url_cache,
)
import llm.ollama as _llm_ollama  # for _url_cache / _url_cache_ts access in /models endpoints
from llm.repair import repair_json, safe_parse_json  # noqa: E402
from llm.extract import (  # noqa: E402
    extract_code, extract_files, format_files_for_prompt,
    LLM_ARTIFACTS, FILE_MARKER,
)

if not PROMPT_INDEX.exists():
    PROMPT_INDEX.write_text("[]")

if not EMBED_CACHE.exists():
    EMBED_CACHE.write_text("{}")

if not NEGATIVE_MEMORY.exists():
    NEGATIVE_MEMORY.write_text("[]")

if not MODEL_STATS.exists():
    MODEL_STATS.write_text("{}")

if not SESSION_LOG.exists():
    SESSION_LOG.write_text("[]")

# create default identity.md if missing
if not IDENTITY_FILE.exists():
    IDENTITY_FILE.write_text(
        "# Identity\n\n"
        "You are an autonomous AI code orchestrator.\n"
        "Your purpose is to generate, test, judge, optimize, and deploy code.\n"
        "Prefer built-in modules. Test before deploying. Learn from failures.\n"
    )

# create default primer.md if missing
if not PRIMER_FILE.exists():
    PRIMER_FILE.write_text(
        "# Primer\n\n"
        "## Last updated\n\nNever — initial template.\n\n"
        "## Active project\n\nNo runs recorded yet.\n"
    )

# create default goals.md if missing
if not GOALS_FILE.exists():
    GOALS_FILE.write_text(
        "# Goals\n\n"
        "## Active goals\n\nNo goals defined yet.\n\n"
        "## Roadmap\n\nNo roadmap items yet.\n"
    )

# Initialize Gates safety system
init_gates()

_run_counter_since_dream = 0



SAFE_PKG_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.\[\]<>=!,]+$")

# LLM_ARTIFACTS and FILE_MARKER are imported from llm.extract above
SAFE_FILENAME = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]+$")


# ------------------------------------------------
# JSON SCHEMAS FOR STRUCTURED OUTPUT
# ------------------------------------------------

# Schemas loaded from agents/ configs — fall back to inline defaults if loader fails
def _load_agent_schema(role, fallback):
    try:
        agent = load_agent(role)
        if agent.schema:
            return agent.schema
    except Exception:
        pass
    return fallback

PLAN_SCHEMA = _load_agent_schema("planner", {
    "type": "object",
    "properties": {
        "language": {"type": "string"}, "entrypoint": {"type": "string"},
        "project_type": {"type": "string"}, "execution_mode": {"type": "string"},
        "port": {"type": "integer"},
        "files": {"type": "object", "additionalProperties": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["language", "entrypoint", "project_type", "execution_mode", "files", "dependencies", "steps"]
})

JUDGE_SCHEMA = _load_agent_schema("judge", {
    "type": "object",
    "properties": {
        "correctness": {"type": "number"}, "robustness": {"type": "number"},
        "security": {"type": "number"}, "performance": {"type": "number"},
        "structure": {"type": "number"}, "overall": {"type": "number"},
        "improvements": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["correctness", "robustness", "security", "performance", "overall", "improvements"]
})

TOOL_DISPATCH_SCHEMA = _load_agent_schema("tool_dispatch", {
    "type": "object",
    "properties": {
        "tools": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "args": {"type": "object", "additionalProperties": {"type": "string"}}},
            "required": ["name"]
        }}
    },
    "required": ["tools"]
})



# ------------------------------------------------
# NOTIFICATION HELPERS (multi-path with fallback)
# ------------------------------------------------
# NOTIFICATION HELPERS
# ------------------------------------------------

from notifications.send import (  # noqa: E402
    send_notification, _send_gotify, _send_ntfy,
    notify_run_complete, notify_run_started,
    send_quick_actions_notification, send_api_cheatsheet_notification,
)


# ------------------------------------------------
# MEMORY
# ------------------------------------------------

def load_prompt_index():
    return locked_read_json(PROMPT_INDEX, [])

def save_prompt_index(data):
    locked_write_json(PROMPT_INDEX, data)

def load_embed_cache():
    return locked_read_json(EMBED_CACHE, {})

def save_embed_cache(data):
    locked_write_json(EMBED_CACHE, data)

def load_negative_memory():
    return locked_read_json(NEGATIVE_MEMORY, [])

def save_negative_memory(data):
    locked_write_json(NEGATIVE_MEMORY, data)

def load_model_stats():
    return locked_read_json(MODEL_STATS, {})

def save_model_stats(data):
    locked_write_json(MODEL_STATS, data)


# ------------------------------------------------
# EMBEDDINGS
# ------------------------------------------------

def generate_embedding(text):

    cache = load_embed_cache()

    if text in cache:
        return cache[text]

    try:

        r = requests.post(
            OLLAMA_EMBED,
            json={
                "model": "nomic-embed-text",
                "prompt": text
            },
            timeout=TIMEOUT_EMBEDDING
        )

        r.raise_for_status()
        data = r.json()

        if "embedding" in data:
            emb = data["embedding"]
        elif "data" in data and len(data["data"]) > 0:
            emb = data["data"][0]["embedding"]
        else:
            raise RuntimeError(f"Invalid embedding response: {list(data.keys())}")

        cache[text] = emb

        # evict oldest entries if cache exceeds limit
        if len(cache) > MAX_EMBED_CACHE_ENTRIES:
            keys = list(cache.keys())
            for k in keys[:len(cache) - MAX_EMBED_CACHE_ENTRIES]:
                del cache[k]

        save_embed_cache(cache)

        return emb

    except requests.exceptions.Timeout:
        print("Embedding error: request timed out")
        return [0.0] * 768

    except requests.exceptions.ConnectionError as e:
        print(f"Embedding error: cannot reach ollama: {e}")
        return [0.0] * 768

    except (json.JSONDecodeError, RuntimeError) as e:
        print(f"Embedding error: bad response: {e}")
        return [0.0] * 768


def cosine_similarity(a, b):

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))

    if norm_a == 0 or norm_b == 0:
        return 0

    return dot / (norm_a * norm_b)


def find_similar(prompt):

    emb = generate_embedding(prompt)
    index = load_prompt_index()
    matches = []

    for entry in index:
        if "embedding" not in entry:
            continue
        score = cosine_similarity(emb, entry["embedding"])
        if score >= SIMILARITY_THRESHOLD:
            matches.append((score, entry))

    matches.sort(reverse=True, key=lambda x: x[0])
    return matches[:3]


def update_memory(prompt, embedding, run_id, score, project, language="python",
                  success=True, winning_model="", troubleshoot_attempts=0,
                  project_type="script"):

    index = load_prompt_index()

    index.append({
        "prompt": prompt,
        "embedding": embedding,
        "run_id": run_id,
        "score": score,
        "project": project,
        "language": language,
        "project_type": project_type,
        "success": success,
        "winning_model": winning_model,
        "troubleshoot_attempts": troubleshoot_attempts,
        "timestamp": datetime.utcnow().isoformat()
    })

    # cap prompt index to prevent unbounded growth
    if len(index) > MAX_PROMPT_INDEX_ENTRIES:
        index = index[-MAX_PROMPT_INDEX_ENTRIES:]

    save_prompt_index(index)


def update_negative_memory(prompt, embedding, run_id, project, language,
                           error_summary, failure_stage, models_tried,
                           project_type="script"):
    """
    Record a failure so the planner can avoid repeating mistakes.
    failure_stage: 'generation', 'verification', 'execution', 'troubleshoot'
    """

    entries = load_negative_memory()

    entries.append({
        "prompt": prompt,
        "embedding": embedding,
        "run_id": run_id,
        "project": project,
        "language": language,
        "project_type": project_type,
        "error_summary": error_summary[:500],
        "failure_stage": failure_stage,
        "models_tried": models_tried,
        "timestamp": datetime.utcnow().isoformat()
    })

    # keep last 200 entries to avoid unbounded growth
    if len(entries) > 200:
        entries = entries[-200:]

    save_negative_memory(entries)


def find_negative_matches(prompt):
    """Find past failures for similar prompts."""

    emb = generate_embedding(prompt)
    entries = load_negative_memory()
    matches = []

    for entry in entries:
        if "embedding" not in entry:
            continue
        score = cosine_similarity(emb, entry["embedding"])
        # slightly lower threshold than positive memory — we want to cast a wider net
        if score >= 0.88:
            matches.append((score, entry))

    matches.sort(reverse=True, key=lambda x: x[0])
    return matches[:3]


def update_model_stats(model, role, language, score, was_winner, succeeded,
                       project_type="script", generation_time_s=0):
    """
    Track per-model performance.
    role: 'generator', 'optimizer', 'troubleshooter'
    """

    stats = load_model_stats()

    if model not in stats:
        stats[model] = {
            "total_runs": 0,
            "total_score": 0,
            "wins": 0,
            "failures": 0,
            "by_language": {},
            "by_role": {},
            "by_project_type": {},
            "recent_scores": []  # last 20 scores for trend
        }

    s = stats[model]
    s["total_runs"] += 1
    s["total_score"] += score

    if was_winner:
        s["wins"] += 1

    if not succeeded:
        s["failures"] += 1

    # by language
    if language not in s["by_language"]:
        s["by_language"][language] = {"runs": 0, "total_score": 0, "wins": 0, "failures": 0}
    lang_s = s["by_language"][language]
    lang_s["runs"] += 1
    lang_s["total_score"] += score
    if was_winner:
        lang_s["wins"] += 1
    if not succeeded:
        lang_s["failures"] += 1

    # by role
    if role not in s["by_role"]:
        s["by_role"][role] = {"runs": 0, "total_score": 0, "wins": 0}
    role_s = s["by_role"][role]
    role_s["runs"] += 1
    role_s["total_score"] += score
    if was_winner:
        role_s["wins"] += 1

    # by project type
    if project_type not in s["by_project_type"]:
        s["by_project_type"][project_type] = {"runs": 0, "total_score": 0, "wins": 0}
    pt_s = s["by_project_type"][project_type]
    pt_s["runs"] += 1
    pt_s["total_score"] += score
    if was_winner:
        pt_s["wins"] += 1

    # recent scores (rolling window of 20)
    s["recent_scores"].append({
        "score": score,
        "language": language,
        "role": role,
        "timestamp": datetime.utcnow().isoformat()
    })
    if len(s["recent_scores"]) > 20:
        s["recent_scores"] = s["recent_scores"][-20:]

    save_model_stats(stats)


def get_model_recommendation(language, project_type):
    """
    Analyze model stats and return a recommendation string for the planner.
    Returns empty string if not enough data.
    """

    stats = load_model_stats()

    if not stats:
        return ""

    recommendations = []

    for model, s in stats.items():
        if s["total_runs"] < 2:
            continue

        avg_score = s["total_score"] / s["total_runs"] if s["total_runs"] > 0 else 0
        win_rate = (s["wins"] / s["total_runs"] * 100) if s["total_runs"] > 0 else 0

        # language-specific stats
        lang_info = ""
        if language in s["by_language"]:
            lang_s = s["by_language"][language]
            if lang_s["runs"] >= 2:
                lang_avg = lang_s["total_score"] / lang_s["runs"]
                lang_fail = lang_s["failures"]
                lang_info = f" ({language}: avg {lang_avg:.1f}, {lang_fail} failures)"

        recommendations.append({
            "model": model,
            "avg_score": avg_score,
            "win_rate": win_rate,
            "runs": s["total_runs"],
            "failures": s["failures"],
            "lang_info": lang_info
        })

    if not recommendations:
        return ""

    recommendations.sort(key=lambda x: x["avg_score"], reverse=True)

    lines = ["MODEL PERFORMANCE (from past runs):"]
    for r in recommendations[:5]:
        lines.append(
            f"  {r['model']}: avg {r['avg_score']:.1f}/10, "
            f"win rate {r['win_rate']:.0f}%, "
            f"{r['runs']} runs, {r['failures']} failures"
            f"{r['lang_info']}"
        )

    return "\n".join(lines)


def build_memory_context(prompt, language, project_type):
    """
    Build a comprehensive memory context string for the planner,
    combining positive memory, negative memory, and model stats.
    """

    sections = []

    # positive matches
    similar = find_similar(prompt)
    if similar:
        lines = ["SUCCESSFUL PAST SOLUTIONS:"]
        for sim_score, entry in similar:
            lang = entry.get("language", "?")
            s = entry.get("score", 0)
            model = entry.get("winning_model", "?")
            ts_attempts = entry.get("troubleshoot_attempts", 0)
            p = entry.get("prompt", "")[:100]
            line = f"  - \"{p}\" [{lang}] score={s}"
            if model and model != "?":
                line += f" model={model}"
            if ts_attempts > 0:
                line += f" (needed {ts_attempts} troubleshoot fixes)"
            lines.append(line)
        sections.append("\n".join(lines))

    # negative matches
    neg_matches = find_negative_matches(prompt)
    if neg_matches:
        lines = ["PAST FAILURES (avoid these approaches):"]
        for sim_score, entry in neg_matches:
            lang = entry.get("language", "?")
            stage = entry.get("failure_stage", "?")
            err = entry.get("error_summary", "?")[:150]
            models = entry.get("models_tried", [])
            p = entry.get("prompt", "")[:100]
            line = f"  - \"{p}\" [{lang}] failed at {stage}: {err}"
            if models:
                line += f" (models: {', '.join(models[:3])})"
            lines.append(line)
        sections.append("\n".join(lines))

    # model recommendations
    model_rec = get_model_recommendation(language, project_type)
    if model_rec:
        sections.append(model_rec)

    return "\n\n".join(sections) if sections else ""


# ------------------------------------------------
# LAYER 1: IDENTITY (static rules, double-read)
# ------------------------------------------------

def load_identity():
    """Load identity.md. Returns the content or a fallback."""
    try:
        return IDENTITY_FILE.read_text().strip()
    except OSError:
        return "You are an autonomous AI code orchestrator."


# ------------------------------------------------
# LAYER 2: PRIMER (session state, auto-rewriting)
# ------------------------------------------------

def load_primer():
    """Load primer.md — the current session state."""
    try:
        return PRIMER_FILE.read_text().strip()
    except OSError:
        return ""


def rewrite_primer(run_id, project_name, language, project_type, score,
                   entrypoint, files, execution, deploy_path, prompt,
                   plan, troubleshoot_attempts, winning_model, env_summary=""):
    """
    Rewrite primer.md after each run to keep session context current.
    This is the orchestrator's short-term working memory.
    """

    success = execution.get("returncode", -1) == 0
    status_str = "succeeded" if success else "failed"

    # gather recent run history (last 5 from positive memory)
    index = load_prompt_index()
    recent = sorted(
        [e for e in index if e.get("timestamp")],
        key=lambda x: x.get("timestamp", ""),
        reverse=True
    )[:5]

    recent_lines = []
    for entry in recent:
        ts = entry.get("timestamp", "?")[:16]
        p = entry.get("prompt", "?")[:80]
        s = entry.get("score", 0)
        lang = entry.get("language", "?")
        ok = "ok" if entry.get("success", False) else "FAIL"
        model = entry.get("winning_model", "?")
        recent_lines.append(f"- [{ts}] {p} | {lang} | score {s} | {ok} | {model}")

    recent_str = "\n".join(recent_lines) if recent_lines else "No recent runs."

    # gather open blockers from negative memory
    neg_entries = load_negative_memory()
    recent_failures = sorted(
        [e for e in neg_entries if e.get("timestamp")],
        key=lambda x: x.get("timestamp", ""),
        reverse=True
    )[:3]

    blocker_lines = []
    for entry in recent_failures:
        p = entry.get("prompt", "?")[:60]
        stage = entry.get("failure_stage", "?")
        err = entry.get("error_summary", "?")[:100]
        blocker_lines.append(f"- \"{p}\" — failed at {stage}: {err}")

    blockers_str = "\n".join(blocker_lines) if blocker_lines else "None known."

    # judge improvements from the last run
    improvements = plan.get("steps", [])
    improvements_str = "\n".join(f"- {s}" for s in improvements[:5]) if improvements else "None specified."

    primer_content = f"""# Primer

> Auto-rewritten after run {run_id}

## Last updated

{datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}

## Active project

**{project_name}** — {status_str} (score {score})
- Language: {language} ({project_type})
- Entrypoint: {entrypoint}
- Files: {', '.join(files) if isinstance(files, list) else ', '.join(files.keys()) if isinstance(files, dict) else str(files)}
- Model: {winning_model}
- Troubleshoot attempts: {troubleshoot_attempts}
- Deployed to: {deploy_path or 'not deployed'}

## Last prompt

{prompt}

## Recent runs

{recent_str}

## Environment summary

{env_summary or 'Not captured this run.'}

## Open blockers

{blockers_str}

## Suggested next steps

{improvements_str}
"""

    try:
        PRIMER_FILE.write_text(primer_content)
    except OSError as e:
        print(f"WARNING: could not rewrite primer.md: {e}")


# ------------------------------------------------
# GOAL MEMORY
# ------------------------------------------------

def load_goals():
    """Load goals.md — project intent and roadmap."""
    try:
        return GOALS_FILE.read_text().strip()
    except OSError:
        return ""


def update_goal_status(goal_title, new_status=None, new_phase=None, add_decision=None):
    """Update a specific goal in goals.md. Simple text-based patching."""

    try:
        content = GOALS_FILE.read_text()
    except OSError:
        return False

    if new_phase and f"### {goal_title}" in content:
        content = re.sub(
            r"(### " + re.escape(goal_title) + r".*?- \*\*Phase\*\*: ).*",
            r"\g<1>" + new_phase,
            content,
            flags=re.DOTALL
        )

    if new_status and f"### {goal_title}" in content:
        content = re.sub(
            r"(### " + re.escape(goal_title) + r".*?- \*\*Status\*\*: ).*",
            r"\g<1>" + new_status,
            content,
            flags=re.DOTALL
        )

    if add_decision and f"### {goal_title}" in content:
        content = content.replace(
            "- **Key decisions made**:",
            f"- **Key decisions made**:\n  - {add_decision}"
        )

    try:
        GOALS_FILE.write_text(content)
        return True
    except OSError:
        return False


# ------------------------------------------------
# SESSION TRACKING
# ------------------------------------------------

SESSION_WINDOW_MINUTES = 60  # runs within this window are grouped into a session


def load_session_log():
    return locked_read_json(SESSION_LOG, [])


def save_session_log(data):
    locked_write_json(SESSION_LOG, data)


def record_session(run_id, project_name, prompt, language, score, success,
                   winning_model, troubleshoot_attempts):
    """
    Record a run into the session log. Runs within SESSION_WINDOW_MINUTES
    of each other are grouped into the same session.
    """

    sessions = load_session_log()
    now = datetime.utcnow()
    now_str = now.isoformat()

    run_entry = {
        "run_id": run_id,
        "project": project_name,
        "prompt": prompt[:200],
        "language": language,
        "score": score,
        "success": success,
        "model": winning_model,
        "troubleshoot_attempts": troubleshoot_attempts,
        "timestamp": now_str
    }

    # check if we should append to the latest session or start a new one
    if sessions:
        last_session = sessions[-1]
        last_ts_str = last_session.get("last_activity", "")
        try:
            last_ts = datetime.fromisoformat(last_ts_str)
            gap_minutes = (now - last_ts).total_seconds() / 60.0

            if gap_minutes <= SESSION_WINDOW_MINUTES:
                last_session["runs"].append(run_entry)
                last_session["last_activity"] = now_str
                last_session["run_count"] = len(last_session["runs"])
                save_session_log(sessions)
                return last_session.get("session_id", "?")
        except (ValueError, TypeError):
            pass

    # start a new session
    session_id = f"session-{now.strftime('%Y%m%d-%H%M')}"
    new_session = {
        "session_id": session_id,
        "started": now_str,
        "last_activity": now_str,
        "run_count": 1,
        "runs": [run_entry]
    }
    sessions.append(new_session)

    # keep last 100 sessions
    if len(sessions) > 100:
        sessions = sessions[-100:]

    save_session_log(sessions)
    return session_id



# ------------------------------------------------
# PER-NODE TARGET IDENTITY FILES
# ------------------------------------------------

TARGET_IDENTITY_DIR = Path(MEMORY_DIR) / "targets"
TARGET_IDENTITY_DIR.mkdir(parents=True, exist_ok=True)


def _create_default_target_identities():
    """Create a default identity.md for each SSH target if not present."""
    for target_name, target_cfg in SSH_TARGETS.items():
        identity_path = TARGET_IDENTITY_DIR / f"{target_name}.md"
        if not identity_path.exists():
            identity_path.write_text(
                f"# Target: {target_name}\n\n"
                f"- **Host**: {target_cfg['host']}\n"
                f"- **User**: {target_cfg['username']}\n"
                f"- **Role**: General-purpose deployment target\n\n"
                f"## Hardware\n\n"
                f"Not yet profiled. Will be updated after first run.\n\n"
                f"## Notes\n\n"
                f"Add target-specific notes here (e.g. connected sensors, "
                f"special packages, resource limits, preferred task types).\n"
            )


_create_default_target_identities()


def load_target_identity(target_name):
    """Load the identity.md for a specific SSH target. Returns content or empty string."""
    identity_path = TARGET_IDENTITY_DIR / f"{target_name}.md"
    try:
        return identity_path.read_text().strip()
    except OSError:
        return ""


def save_target_identity(target_name, content):
    """Save updated identity for a target."""
    identity_path = TARGET_IDENTITY_DIR / f"{target_name}.md"
    try:
        identity_path.write_text(content)
        return True
    except OSError as e:
        print(f"WARNING: could not write target identity for {target_name}: {e}")
        return False


def auto_update_target_identity(target_name, env_data, run_id):
    """
    Auto-enrich a target identity file with environment data after a run.
    Only fills in the Hardware section if it still says 'Not yet profiled'.
    """
    identity_path = TARGET_IDENTITY_DIR / f"{target_name}.md"
    try:
        content = identity_path.read_text()
    except OSError:
        return

    if "Not yet profiled" not in content:
        return  # already populated, don't overwrite manual edits

    os_info = env_data.get("os", "unknown")
    arch = env_data.get("arch", "unknown")
    kernel = env_data.get("kernel", "unknown")
    python_ver = env_data.get("python", "unknown")
    node_ver = env_data.get("node", "unknown")
    cpu_count = env_data.get("cpu", "unknown")
    mem_raw = env_data.get("memory", "")
    memory = mem_raw.split("\n")[0] if mem_raw else "unknown"

    hardware_section = (
        f"## Hardware\n\n"
        f"- **OS**: {os_info}\n"
        f"- **Arch**: {arch}\n"
        f"- **Kernel**: {kernel}\n"
        f"- **Python**: {python_ver}\n"
        f"- **Node.js**: {node_ver}\n"
        f"- **CPU cores**: {cpu_count}\n"
        f"- **Memory**: {memory}\n"
        f"- **Profiled at**: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
    )

    content = content.replace(
        "## Hardware\n\nNot yet profiled. Will be updated after first run.",
        hardware_section
    )

    try:
        identity_path.write_text(content)
        log(run_id, f"auto-profiled target identity: {target_name}")
    except OSError:
        pass


# ------------------------------------------------
# LAYER 3: LIVE CONTEXT (gathered at launch)
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
# LAYER 4: HINDSIGHT INTEGRATION
# ------------------------------------------------

def hindsight_request(method, endpoint, payload=None, timeout=None):
    """Make a request to the Hindsight API. Returns response dict or None."""

    if not HINDSIGHT_ENABLED:
        return None

    url = f"{HINDSIGHT_URL}{endpoint}"
    t = timeout or HINDSIGHT_TIMEOUT

    try:
        if method == "GET":
            r = requests.get(url, timeout=t)
        elif method == "PUT":
            r = requests.put(url, json=payload or {}, timeout=t)
        else:
            r = requests.post(url, json=payload, timeout=t)

        r.raise_for_status()
        return r.json()

    except requests.exceptions.Timeout:
        print(f"Hindsight timeout: {endpoint}")
        return None
    except requests.exceptions.ConnectionError:
        print(f"Hindsight unreachable: {HINDSIGHT_URL}")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"Hindsight HTTP error on {endpoint}: {e} — {r.text[:200] if r else ''}")
        return None
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Hindsight bad response on {endpoint}: {e}")
        return None


def hindsight_ensure_bank():
    """Create the orchestrator bank if it doesn't exist. Called once at startup."""

    if not HINDSIGHT_ENABLED:
        return

    try:
        # check if bank exists
        result = hindsight_request("GET", "/v1/default/banks", timeout=10)
        if result:
            banks = result.get("banks", [])
            for bank in banks:
                if bank.get("bank_id") == HINDSIGHT_BANK:
                    print(f"Hindsight bank '{HINDSIGHT_BANK}' exists")
                    return

        # create the bank
        print(f"Creating Hindsight bank '{HINDSIGHT_BANK}'...")
        create_result = hindsight_request(
            "PUT",
            f"/v1/default/banks/{HINDSIGHT_BANK}",
            {"name": HINDSIGHT_BANK, "mission": "AI code orchestrator memory"},
            timeout=15
        )
        if create_result:
            print(f"Hindsight bank '{HINDSIGHT_BANK}' created successfully")
        else:
            print(f"Hindsight bank creation returned no response (may already exist)")

    except (requests.exceptions.RequestException, OSError, ValueError) as e:
        print(f"Hindsight bank setup failed (non-fatal): {e}")


# attempt bank creation on startup (non-blocking, best-effort)
if HINDSIGHT_ENABLED:
    try:
        hindsight_ensure_bank()
    except (requests.exceptions.RequestException, OSError, ValueError):
        print("Hindsight not available at startup (will retry on first use)")


def hindsight_retain(content, run_id, metadata=None):
    """
    Store a memory in Hindsight.
    Content should be a natural-language narrative of what happened.
    Hindsight will extract facts, entities, and relationships automatically.
    Uses the RetainRequest format: {"items": [{"content": "..."}]}
    """

    if not HINDSIGHT_ENABLED:
        return None

    payload = {
        "items": [
            {
                "content": content,
                "context": "orchestrator-run",
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
        ],
        "async": True  # process in background, don't block
    }

    log(run_id, "hindsight: retaining run memory")

    result = hindsight_request(
        "POST",
        f"/v1/default/banks/{HINDSIGHT_BANK}/memories",
        payload,
        timeout=TIMEOUT_HINDSIGHT_RETAIN
    )

    if result:
        log(run_id, "hindsight: memory retained successfully")
    else:
        log(run_id, "hindsight: retain failed (non-fatal)")

    return result


def hindsight_recall(query, run_id, max_tokens=2000):
    """
    Recall relevant memories from Hindsight.
    Returns a list of relevant memory fragments or None.
    """

    if not HINDSIGHT_ENABLED:
        return None

    payload = {
        "query": query,
        "max_tokens": max_tokens,
        "types": ["world", "experience", "observation"]
    }

    log(run_id, "hindsight: recalling relevant memories")

    result = hindsight_request(
        "POST",
        f"/v1/default/banks/{HINDSIGHT_BANK}/memories/recall",
        payload,
        timeout=TIMEOUT_HINDSIGHT_RECALL
    )

    if result:
        # extract the text content from recall response
        memories = result.get("memories", [])
        if memories:
            log(run_id, f"hindsight: recalled {len(memories)} memory fragment(s)")
        else:
            log(run_id, "hindsight: no relevant memories found")
        return result
    else:
        log(run_id, "hindsight: recall failed (non-fatal)")
        return None


def hindsight_reflect(query, run_id):
    """
    Ask Hindsight to reflect on accumulated memories and synthesize insights.
    This is the 'learning' operation — it forms opinions and observations.
    """

    if not HINDSIGHT_ENABLED:
        return None

    payload = {
        "query": query
    }

    log(run_id, f"hindsight: reflecting on '{query[:80]}'")

    result = hindsight_request(
        "POST",
        f"/v1/default/banks/{HINDSIGHT_BANK}/reflect",
        payload,
        timeout=TIMEOUT_HINDSIGHT_REFLECT
    )

    if result:
        log(run_id, "hindsight: reflection complete")
    else:
        log(run_id, "hindsight: reflect failed (non-fatal)")

    return result


def hindsight_get_mental_models(run_id="ctx"):
    """Fetch all mental model summaries from Hindsight."""

    if not HINDSIGHT_ENABLED:
        return []

    result = hindsight_request("GET", f"/v1/default/banks/{HINDSIGHT_BANK}/mental-models", timeout=15)

    if not result:
        return []

    items = result.get("items", [])
    ready = [m for m in items if m.get("content") and m["content"] != "Generating content..."]
    log(run_id, f"hindsight: {len(ready)}/{len(items)} mental models ready")
    return ready


def format_mental_models_for_planner(models):
    """Format mental model content into a context string for the planner."""

    if not models:
        return ""

    lines = ["MENTAL MODELS (synthesized insights from Hindsight):"]
    for m in models:
        name = m.get("name", "Unknown")
        content = m.get("content", "")
        if content:
            # truncate each model to keep total context manageable
            lines.append(f"\n### {name}\n{content[:800]}")

    return "\n".join(lines) if len(lines) > 1 else ""


def hindsight_retain_file(file_path, run_id, description=""):
    """
    Ingest a file (PDF, text, markdown) into Hindsight via the /files/retain endpoint.
    Hindsight extracts content and adds it to the memory bank.
    """

    if not HINDSIGHT_ENABLED:
        return None

    if not os.path.isfile(file_path):
        log(run_id, f"hindsight: file not found: {file_path}")
        return None

    log(run_id, f"hindsight: ingesting file {file_path}")

    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            data = {}
            if description:
                data["description"] = description

            r = requests.post(
                f"{HINDSIGHT_URL}/v1/default/banks/{HINDSIGHT_BANK}/files/retain",
                files=files,
                data=data,
                timeout=TIMEOUT_HINDSIGHT_RETAIN
            )

            r.raise_for_status()
            result = r.json()
            log(run_id, "hindsight: file ingested successfully")
            return result

    except requests.exceptions.RequestException as e:
        log(run_id, f"hindsight: file ingest failed: {e}")
        return None
    except (OSError, ValueError) as e:
        log(run_id, f"hindsight: file read error: {e}")
        return None


def build_hindsight_retain_content(run_id, project_name, prompt, language,
                                    project_type, score, success, winning_model,
                                    troubleshoot_attempts, entrypoint, files,
                                    execution, deploy_path, target, plan):
    """
    Build a natural-language narrative for Hindsight to retain.
    Hindsight works best with rich, descriptive content — it extracts
    structured facts from prose automatically.
    """

    status = "succeeded" if success else "failed"
    exit_code = execution.get("returncode", "?")
    stderr_preview = execution.get("stderr", "")[:300]
    stdout_preview = execution.get("stdout", "")[:200]
    file_list = ", ".join(files.keys()) if isinstance(files, dict) else str(files)
    dep_list = ", ".join(plan.get("dependencies", [])) or "none"

    content = (
        f"Orchestration run {run_id} for project '{project_name}' {status}. "
        f"Task: \"{prompt}\". "
        f"Language: {language} ({project_type}). "
        f"Entrypoint: {entrypoint}. Files: {file_list}. "
        f"Dependencies: {dep_list}. "
        f"Target: {target}. "
        f"Winning model: {winning_model} with score {score}/10. "
        f"Troubleshoot attempts: {troubleshoot_attempts}. "
    )

    if success:
        content += f"Code executed successfully (exit code 0). "
        if deploy_path:
            content += f"Deployed to {deploy_path}. "
        if stdout_preview:
            content += f"Output: {stdout_preview}. "
    else:
        content += (
            f"Execution failed with exit code {exit_code}. "
            f"Error: {stderr_preview}. "
        )

    # add model comparison context if we have candidates
    content += (
        f"The planner chose {language} for this task type. "
        f"The optimizer improved the score from the initial generation. "
    )

    return content


def format_hindsight_recall_for_planner(recall_result):
    """Format Hindsight recall results into a string for the planner context."""

    if not recall_result:
        return ""

    lines = ["DEEP MEMORY (from Hindsight knowledge graph):"]
    found = False

    # Hindsight recall can return different top-level keys
    # Try: memories, facts, results, items, text
    for key in ["memories", "facts", "results", "items"]:
        items = recall_result.get(key, [])
        if items:
            for item in items[:5]:
                text = ""
                if isinstance(item, dict):
                    text = (item.get("content") or item.get("text") or
                            item.get("summary") or item.get("fact") or
                            item.get("value") or "")
                elif isinstance(item, str):
                    text = item
                if text:
                    lines.append(f"  - {text[:250]}")
                    found = True
            break

    # Also check for a top-level "text" or "response" field
    if not found:
        for key in ["text", "response", "answer"]:
            text = recall_result.get(key, "")
            if text and isinstance(text, str):
                lines.append(f"  - {text[:500]}")
                found = True
                break

    return "\n".join(lines) if found else ""



# ------------------------------------------------
# VAULT WRITER (L5)
# ------------------------------------------------

def _vault_ensure_dirs():
    """Create local vault directory structure."""
    for subdir in ["runs", "projects", "models", "targets", "errors", "daily"]:
        Path(VAULT_LOCAL_DIR, subdir).mkdir(parents=True, exist_ok=True)

if VAULT_ENABLED:
    _vault_ensure_dirs()


def _vault_safe_name(name):
    """Make a string safe for filenames. Colons become dashes, etc."""
    safe = re.sub(r"[^a-zA-Z0-9_\-\.]", "-", str(name))
    safe = re.sub(r"-{2,}", "-", safe).strip("-")
    return safe[:80] if safe else "unknown"


def vault_write_local(subdir, filename, content):
    """Write a markdown file to the local vault directory."""
    if not VAULT_ENABLED:
        return None

    filepath = Path(VAULT_LOCAL_DIR) / subdir / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)

    try:
        filepath.write_text(content)
        return str(filepath)
    except OSError as e:
        print(f"Vault write failed: {filepath}: {e}")
        return None


def vault_sync_to_remote(run_id="vault-sync"):
    """
    Sync the local vault directory to the remote NoteDiscovery host via rsync/scp.
    Uses rsync if available, falls back to scp -r.
    """
    if not VAULT_ENABLED or not VAULT_SYNC_ENABLED or not VAULT_REMOTE_HOST:
        return False

    log(run_id, f"vault: syncing to {VAULT_REMOTE_HOST}:{VAULT_REMOTE_DIR}")

    # try rsync first (efficient, incremental)
    rsync_cmd = [
        "rsync", "-az", "--delete",
        "-e", f"ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 -i {VAULT_REMOTE_KEY}",
        f"{VAULT_LOCAL_DIR}/",
        f"{VAULT_REMOTE_USER}@{VAULT_REMOTE_HOST}:{VAULT_REMOTE_DIR}/"
    ]

    try:
        r = subprocess.run(rsync_cmd, capture_output=True, text=True, timeout=TIMEOUT_VAULT_SYNC)
        if r.returncode == 0:
            log(run_id, "vault: rsync completed successfully")
            return True
        else:
            log(run_id, f"vault: rsync failed ({r.returncode}): {r.stderr[:200]}")
    except FileNotFoundError:
        log(run_id, "vault: rsync not available, falling back to scp")
    except subprocess.TimeoutExpired:
        log(run_id, "vault: rsync timed out")

    # fallback: scp -r
    scp_cmd = [
        "scp", "-r",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=30",
        "-i", VAULT_REMOTE_KEY,
        f"{VAULT_LOCAL_DIR}/.",
        f"{VAULT_REMOTE_USER}@{VAULT_REMOTE_HOST}:{VAULT_REMOTE_DIR}/"
    ]

    try:
        r = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=TIMEOUT_VAULT_SYNC)
        if r.returncode == 0:
            log(run_id, "vault: scp completed successfully")
            return True
        else:
            log(run_id, f"vault: scp failed ({r.returncode}): {r.stderr[:200]}")
            return False
    except subprocess.TimeoutExpired:
        log(run_id, "vault: scp timed out")
        return False


def vault_sync_file(subdir, filename, run_id="vault-sync"):
    """Sync a single file to the remote vault (faster than full rsync for incremental writes)."""
    if not VAULT_ENABLED or not VAULT_SYNC_ENABLED or not VAULT_REMOTE_HOST:
        return False

    local_path = Path(VAULT_LOCAL_DIR) / subdir / filename
    if not local_path.exists():
        return False

    remote_path = f"{VAULT_REMOTE_DIR}/{subdir}/{filename}"
    remote_dir = f"{VAULT_REMOTE_DIR}/{subdir}"

    # ensure remote directory exists
    mkdir_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
        "-i", VAULT_REMOTE_KEY,
        f"{VAULT_REMOTE_USER}@{VAULT_REMOTE_HOST}",
        f"mkdir -p {shlex.quote(remote_dir)}"
    ]

    try:
        subprocess.run(mkdir_cmd, capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    scp_cmd = [
        "scp",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
        "-i", VAULT_REMOTE_KEY,
        str(local_path),
        f"{VAULT_REMOTE_USER}@{VAULT_REMOTE_HOST}:{remote_path}"
    ]

    try:
        r = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False




def vault_sync_to_nas(run_id="vault-nas"):
    """
    Sync vault to NAS mount point using local rsync.
    The NAS should already be mounted (e.g. via fstab).
    """
    if not VAULT_ENABLED or not VAULT_NAS_ENABLED:
        return False

    nas_path = VAULT_NAS_PATH

    # check mount is accessible
    if not os.path.isdir(os.path.dirname(nas_path)):
        log(run_id, f"vault-nas: mount point not accessible: {os.path.dirname(nas_path)}")
        return False

    # create target dir if needed
    os.makedirs(nas_path, exist_ok=True)

    log(run_id, f"vault-nas: syncing to {nas_path}")

    try:
        r = subprocess.run(
            ["rsync", "-a", "--delete", f"{VAULT_LOCAL_DIR}/", f"{nas_path}/"],
            capture_output=True, text=True, timeout=TIMEOUT_VAULT_NAS_SYNC
        )
        if r.returncode == 0:
            log(run_id, "vault-nas: sync completed")
            return True
        else:
            log(run_id, f"vault-nas: rsync failed: {r.stderr[:200]}")
    except FileNotFoundError:
        # rsync not available, fall back to cp -r
        log(run_id, "vault-nas: rsync not available, using cp")
        try:
            r = subprocess.run(
                ["cp", "-ru", f"{VAULT_LOCAL_DIR}/.", f"{nas_path}/"],
                capture_output=True, text=True, timeout=TIMEOUT_VAULT_NAS_SYNC
            )
            if r.returncode == 0:
                log(run_id, "vault-nas: cp sync completed")
                return True
            else:
                log(run_id, f"vault-nas: cp failed: {r.stderr[:200]}")
        except subprocess.TimeoutExpired:
            log(run_id, "vault-nas: cp timed out")
    except subprocess.TimeoutExpired:
        log(run_id, "vault-nas: rsync timed out")

    return False


# ---- NOTE GENERATORS ----

def vault_write_run_note(run_id, project_name, prompt, language, project_type,
                          score, success, winning_model, troubleshoot_attempts,
                          entrypoint, files, execution, deploy_path, target,
                          plan, best_judge, elapsed_seconds=0):
    """Write a detailed note for a single orchestration run."""

    if not VAULT_ENABLED:
        return

    status = "success" if success else "failed"
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    time_str = datetime.utcnow().strftime("%H:%M UTC")
    safe_project = _vault_safe_name(project_name)
    safe_model = _vault_safe_name(winning_model)
    short_id = run_id[:8]

    filename = f"{date_str}_{safe_project}_{short_id}.md"

    # build file list for display
    file_list = list(files.keys()) if isinstance(files, dict) else [str(files)]

    # extract judge scores
    correctness = best_judge.get("correctness", "?")
    robustness = best_judge.get("robustness", "?")
    security = best_judge.get("security", "?")
    performance = best_judge.get("performance", "?")
    structure = best_judge.get("structure", "?")
    improvements = best_judge.get("improvements", [])

    # stdout/stderr preview
    stdout = execution.get("stdout", "").strip()[:300]
    stderr = execution.get("stderr", "").strip()[:300]
    exit_code = execution.get("returncode", "?")

    # elapsed
    mins = elapsed_seconds // 60
    secs = elapsed_seconds % 60
    elapsed_str = f"{mins}m{secs:02d}s" if mins > 0 else f"{secs}s"

    deps = plan.get("dependencies", [])

    content = f"""---
run_id: {run_id}
project: {project_name}
date: {date_str}
score: {score}
language: {language}
project_type: {project_type}
model: {winning_model}
target: {target}
status: {status}
troubleshoot_attempts: {troubleshoot_attempts}
exit_code: {exit_code}
tags:
  - run
  - {language}
  - {status}
  - {target}
---

# {project_name} — Run {short_id}

| Field | Value |
|-------|-------|
| Project | [[projects/{safe_project}]] |
| Model | [[models/{safe_model}]] |
| Target | [[targets/{_vault_safe_name(target)}]] |
| Score | **{score}/10** |
| Language | {language} ({project_type}) |
| Time | {elapsed_str} |
| Date | {date_str} {time_str} |

## Prompt

{prompt}

## Judge Scores

| Category | Score |
|----------|-------|
| Correctness | {correctness} |
| Robustness | {robustness} |
| Security | {security} |
| Performance | {performance} |
| Structure | {structure} |
| **Overall** | **{score}** |

"""

    if improvements:
        content += "## Improvements Suggested\n\n"
        for imp in improvements[:5]:
            content += f"- {imp}\n"
        content += "\n"

    content += f"""## Execution

- **Exit code**: {exit_code}
- **Entrypoint**: `{entrypoint}`
- **Files**: {', '.join(f'`{f}`' for f in file_list)}
- **Dependencies**: {', '.join(deps) if deps else 'none'}
- **Troubleshoot attempts**: {troubleshoot_attempts}
"""

    if deploy_path:
        content += f"- **Deployed to**: `{deploy_path}`\n"

    if stdout:
        content += f"\n### stdout\n\n```\n{stdout}\n```\n"

    if stderr and not success:
        content += f"\n### stderr\n\n```\n{stderr}\n```\n"

    content += f"""
## Links

- Run ID: `{run_id}`
- Log: `/opt/ai-orchestrator/logs/{run_id}.log`
"""

    vault_write_local("runs", filename, content)
    vault_sync_file("runs", filename, run_id)

    return filename


def vault_write_project_note(project_name, run_id="vault"):
    """
    Write or update the aggregated project note.
    Pulls data from positive + negative memory to build a complete history.
    """

    if not VAULT_ENABLED:
        return

    safe_project = _vault_safe_name(project_name)
    filename = f"{safe_project}.md"

    # gather all runs for this project from memory
    index = load_prompt_index()
    project_runs = [e for e in index if e.get("project") == project_name]
    project_runs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # gather failures
    neg_entries = load_negative_memory()
    project_failures = [e for e in neg_entries if e.get("project") == project_name]

    total_runs = len(project_runs)
    successes = sum(1 for r in project_runs if r.get("success", False))
    avg_score = sum(r.get("score", 0) for r in project_runs) / total_runs if total_runs > 0 else 0

    # most common language
    lang_counts = {}
    for r in project_runs:
        lang = r.get("language", "?")
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    primary_lang = max(lang_counts, key=lang_counts.get) if lang_counts else "unknown"

    # models used
    models_used = list(set(r.get("winning_model", "?") for r in project_runs if r.get("winning_model")))

    content = f"""---
project: {project_name}
total_runs: {total_runs}
success_rate: {round(successes / total_runs * 100) if total_runs > 0 else 0}%
avg_score: {avg_score:.1f}
primary_language: {primary_lang}
last_updated: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
tags:
  - project
  - {primary_lang}
---

# {project_name}

| Metric | Value |
|--------|-------|
| Total runs | {total_runs} |
| Successes | {successes} / {total_runs} |
| Avg score | {avg_score:.1f}/10 |
| Primary language | {primary_lang} |
| Models used | {', '.join(f'[[models/{_vault_safe_name(m)}]]' for m in models_used[:5])} |

## Run History

"""

    for r in project_runs[:20]:
        ts = r.get("timestamp", "?")[:16]
        s = r.get("score", 0)
        ok = "pass" if r.get("success", False) else "fail"
        model = r.get("winning_model", "?")
        rid = r.get("run_id", "?")[:8]
        prompt_preview = r.get("prompt", "?")[:60]
        content += f"- `{ts}` score={s} {ok} [[models/{_vault_safe_name(model)}|{model}]] — {prompt_preview}\n"

    if project_failures:
        content += "\n## Failures\n\n"
        for f in project_failures[:10]:
            ts = f.get("timestamp", "?")[:16]
            stage = f.get("failure_stage", "?")
            err = f.get("error_summary", "?")[:100]
            content += f"- `{ts}` failed at {stage}: {err}\n"

    vault_write_local("projects", filename, content)
    vault_sync_file("projects", filename, run_id)


def vault_write_model_note(model_name, run_id="vault"):
    """Write or update a model performance note."""

    if not VAULT_ENABLED:
        return

    stats = load_model_stats()
    if model_name not in stats:
        return

    s = stats[model_name]
    safe_model = _vault_safe_name(model_name)
    filename = f"{safe_model}.md"

    total = s["total_runs"]
    avg_score = s["total_score"] / total if total > 0 else 0
    win_rate = (s["wins"] / total * 100) if total > 0 else 0
    fail_rate = (s["failures"] / total * 100) if total > 0 else 0

    content = f"""---
model: {model_name}
total_runs: {total}
avg_score: {avg_score:.1f}
win_rate: {win_rate:.0f}%
fail_rate: {fail_rate:.0f}%
last_updated: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
tags:
  - model
---

# {model_name}

| Metric | Value |
|--------|-------|
| Total runs | {total} |
| Avg score | {avg_score:.1f}/10 |
| Win rate | {win_rate:.0f}% |
| Fail rate | {fail_rate:.0f}% |
| Wins | {s['wins']} |
| Failures | {s['failures']} |

## By Language

"""

    for lang, lang_s in s.get("by_language", {}).items():
        runs = lang_s.get("runs", 0)
        lang_avg = lang_s.get("total_score", 0) / runs if runs > 0 else 0
        content += f"- **{lang}**: {runs} runs, avg {lang_avg:.1f}, {lang_s.get('wins', 0)} wins, {lang_s.get('failures', 0)} failures\n"

    content += "\n## By Role\n\n"
    for role, role_s in s.get("by_role", {}).items():
        runs = role_s.get("runs", 0)
        role_avg = role_s.get("total_score", 0) / runs if runs > 0 else 0
        content += f"- **{role}**: {runs} runs, avg {role_avg:.1f}\n"

    content += "\n## Recent Trend\n\n"
    recent = s.get("recent_scores", [])
    if recent:
        for entry in recent[-10:]:
            content += f"- {entry.get('timestamp', '?')[:16]} | {entry.get('role', '?')} | {entry.get('language', '?')} | score {entry.get('score', 0)}\n"
    else:
        content += "No recent data.\n"

    vault_write_local("models", filename, content)
    vault_sync_file("models", filename, run_id)


def vault_write_target_note(target_name, run_id="vault"):
    """Write or update a target node note, combining identity + run history."""

    if not VAULT_ENABLED:
        return

    safe_target = _vault_safe_name(target_name)
    filename = f"{safe_target}.md"

    # load target identity
    identity = load_target_identity(target_name)

    # gather runs on this target from memory
    index = load_prompt_index()
    target_runs = [e for e in index if e.get("project", "")]  # all runs go somewhere

    # we don't track target in prompt_index currently, so we pull from RUN_STATUS
    completed_on_target = []
    for rid, info in RUN_STATUS.items():
        if info.get("target") == target_name and info.get("completed"):
            result = info.get("result", {})
            completed_on_target.append({
                "run_id": rid,
                "project": info.get("project", "?"),
                "score": result.get("score", 0),
                "language": result.get("language", "?"),
                "success": result.get("execution", {}).get("returncode", -1) == 0,
                "model": result.get("winning_model", "?"),
            })

    cfg = SSH_TARGETS.get(target_name, {})

    content = f"""---
target: {target_name}
host: {cfg.get('host', '?')}
user: {cfg.get('username', '?')}
last_updated: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
tags:
  - target
---

# {target_name}

| Field | Value |
|-------|-------|
| Host | {cfg.get('host', '?')} |
| User | {cfg.get('username', '?')} |
| Runs this session | {len(completed_on_target)} |

## Identity

{identity if identity else 'No identity file configured.'}

## Recent Runs (this session)

"""

    if completed_on_target:
        for r in completed_on_target[-15:]:
            ok = "pass" if r.get("success") else "fail"
            content += f"- [[projects/{_vault_safe_name(r['project'])}|{r['project']}]] score={r['score']} {ok} [[models/{_vault_safe_name(r['model'])}|{r['model']}]]\n"
    else:
        content += "No runs recorded this session.\n"

    vault_write_local("targets", filename, content)
    vault_sync_file("targets", filename, run_id)


def vault_write_error_note(error_key, error_summary, language, project_name,
                            model, stage, run_id):
    """Write or append to an error pattern note."""

    if not VAULT_ENABLED:
        return

    safe_key = _vault_safe_name(error_key)
    filename = f"{safe_key}.md"
    filepath = Path(VAULT_LOCAL_DIR) / "errors" / filename

    date_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if filepath.exists():
        # append new occurrence
        try:
            existing = filepath.read_text()
            occurrence = (
                f"\n### {date_str}\n\n"
                f"- Project: [[projects/{_vault_safe_name(project_name)}]]\n"
                f"- Model: [[models/{_vault_safe_name(model)}]]\n"
                f"- Language: {language}\n"
                f"- Stage: {stage}\n"
                f"- Error: `{error_summary[:200]}`\n"
            )
            content = existing.rstrip() + "\n" + occurrence
        except OSError:
            return
    else:
        content = f"""---
error_pattern: {error_key}
first_seen: {date_str}
tags:
  - error
  - {language}
---

# Error: {error_key}

## Occurrences

### {date_str}

- Project: [[projects/{_vault_safe_name(project_name)}]]
- Model: [[models/{_vault_safe_name(model)}]]
- Language: {language}
- Stage: {stage}
- Error: `{error_summary[:200]}`
"""

    vault_write_local("errors", filename, content)
    vault_sync_file("errors", filename, run_id)


def _classify_error(stderr):
    """Extract a short error key from stderr for grouping error patterns."""

    if not stderr:
        return "unknown-error"

    # common Python errors
    patterns = [
        (r"ModuleNotFoundError: No module named '(\w+)'", "module-not-found-{}"),
        (r"ImportError: cannot import name '(\w+)'", "import-error-{}"),
        (r"FileNotFoundError: .*'(.+?)'", "file-not-found"),
        (r"PermissionError", "permission-error"),
        (r"ConnectionRefusedError", "connection-refused"),
        (r"SyntaxError", "syntax-error"),
        (r"IndentationError", "indentation-error"),
        (r"TypeError: (.+?)$", "type-error"),
        (r"NameError: name '(\w+)' is not defined", "name-error-{}"),
        (r"OSError: \[Errno (\d+)\]", "os-error-{}"),
        # Node errors
        (r"Cannot find module '(.+?)'", "node-module-not-found-{}"),
        (r"ReferenceError: (\w+) is not defined", "node-reference-error-{}"),
        # Bash errors
        (r"command not found", "command-not-found"),
        (r"Permission denied", "permission-denied"),
    ]

    for pattern, key_template in patterns:
        match = re.search(pattern, stderr, re.MULTILINE)
        if match:
            groups = match.groups()
            if groups and "{}" in key_template:
                return key_template.format(_vault_safe_name(groups[0][:30]))
            return key_template

    return "runtime-error"


def vault_write_daily_digest(run_id="vault"):
    """Write or update today's daily digest note."""

    if not VAULT_ENABLED:
        return

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    filename = f"{date_str}.md"

    # gather today's completed runs from RUN_STATUS
    today_runs = []
    for rid, info in RUN_STATUS.items():
        if not info.get("completed"):
            continue
        result = info.get("result")
        if not result:
            continue
        today_runs.append({
            "run_id": rid,
            "project": info.get("project", "?"),
            "target": info.get("target", "?"),
            "score": result.get("score", 0),
            "language": result.get("language", "?"),
            "success": result.get("execution", {}).get("returncode", -1) == 0,
            "model": result.get("winning_model", "?"),
            "deployed_to": result.get("deployed_to"),
        })

    total = len(today_runs)
    successes = sum(1 for r in today_runs if r["success"])
    avg_score = sum(r["score"] for r in today_runs) / total if total > 0 else 0

    # models used today
    models_today = list(set(r["model"] for r in today_runs if r["model"]))
    langs_today = list(set(r["language"] for r in today_runs if r["language"]))

    content = f"""---
date: {date_str}
total_runs: {total}
successes: {successes}
avg_score: {avg_score:.1f}
tags:
  - daily
---

# Daily Digest — {date_str}

| Metric | Value |
|--------|-------|
| Total runs | {total} |
| Successes | {successes} / {total} |
| Avg score | {avg_score:.1f}/10 |
| Languages | {', '.join(langs_today) if langs_today else 'none'} |
| Models | {', '.join(f'[[models/{_vault_safe_name(m)}|{m}]]' for m in models_today)} |

## Runs

"""

    for r in today_runs:
        ok = "pass" if r["success"] else "fail"
        short_id = r["run_id"][:8]
        safe_project = _vault_safe_name(r["project"])
        safe_model = _vault_safe_name(r["model"])
        safe_target = _vault_safe_name(r["target"])
        content += (
            f"- [[runs/{date_str}_{safe_project}_{short_id}|{r['project']}]] "
            f"score={r['score']} {ok} "
            f"[[models/{safe_model}|{r['model']}]] "
            f"on [[targets/{safe_target}|{r['target']}]]\n"
        )

    if not today_runs:
        content += "No runs yet today.\n"

    vault_write_local("daily", filename, content)
    vault_sync_file("daily", filename, run_id)


def vault_write_index(run_id="vault"):
    """Write the vault index (dashboard) note with links to everything."""

    if not VAULT_ENABLED:
        return

    date_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # count notes per category
    counts = {}
    for subdir in ["runs", "projects", "models", "targets", "errors", "daily"]:
        dir_path = Path(VAULT_LOCAL_DIR) / subdir
        if dir_path.exists():
            counts[subdir] = len(list(dir_path.glob("*.md")))
        else:
            counts[subdir] = 0

    # list recent run files
    runs_dir = Path(VAULT_LOCAL_DIR) / "runs"
    run_files = sorted(runs_dir.glob("*.md"), reverse=True)[:10] if runs_dir.exists() else []

    # list project files
    projects_dir = Path(VAULT_LOCAL_DIR) / "projects"
    project_files = sorted(projects_dir.glob("*.md")) if projects_dir.exists() else []

    # list model files
    models_dir = Path(VAULT_LOCAL_DIR) / "models"
    model_files = sorted(models_dir.glob("*.md")) if models_dir.exists() else []

    content = f"""---
title: Orchestrator Vault
updated: {date_str}
tags:
  - index
---

# AI Orchestrator — Vault Index

> Last updated: {date_str}

## Quick Stats

| Category | Notes |
|----------|-------|
| Runs | {counts.get('runs', 0)} |
| Projects | {counts.get('projects', 0)} |
| Models | {counts.get('models', 0)} |
| Targets | {counts.get('targets', 0)} |
| Error patterns | {counts.get('errors', 0)} |
| Daily digests | {counts.get('daily', 0)} |

## Recent Runs

"""

    for f in run_files[:10]:
        name = f.stem
        content += f"- [[runs/{name}]]\n"

    content += "\n## Projects\n\n"
    for f in project_files:
        name = f.stem
        content += f"- [[projects/{name}]]\n"

    content += "\n## Models\n\n"
    for f in model_files:
        name = f.stem
        content += f"- [[models/{name}]]\n"

    content += "\n## Targets\n\n"
    for target_name in SSH_TARGETS:
        safe = _vault_safe_name(target_name)
        content += f"- [[targets/{safe}|{target_name}]]\n"

    content += "\n## Daily Digests\n\n"
    daily_dir = Path(VAULT_LOCAL_DIR) / "daily"
    if daily_dir.exists():
        daily_files = sorted(daily_dir.glob("*.md"), reverse=True)[:14]
        for f in daily_files:
            name = f.stem
            content += f"- [[daily/{name}]]\n"

    content += "\n## Error Patterns\n\n"
    errors_dir = Path(VAULT_LOCAL_DIR) / "errors"
    if errors_dir.exists():
        error_files = sorted(errors_dir.glob("*.md"))
        for f in error_files:
            name = f.stem
            content += f"- [[errors/{name}]]\n"

    vault_write_local("", "index.md", content)
    vault_sync_file("", "index.md", run_id)


def vault_after_run(run_id, project_name, prompt, language, project_type,
                     score, success, winning_model, troubleshoot_attempts,
                     entrypoint, files, execution, deploy_path, target,
                     plan, best_judge, elapsed_seconds=0):
    """
    Master function called after each run. Writes all relevant vault notes.
    Non-blocking, best-effort — vault failures never crash the orchestrator.
    """

    if not VAULT_ENABLED:
        return

    try:
        # 1. run note
        vault_write_run_note(
            run_id=run_id, project_name=project_name, prompt=prompt,
            language=language, project_type=project_type, score=score,
            success=success, winning_model=winning_model,
            troubleshoot_attempts=troubleshoot_attempts,
            entrypoint=entrypoint, files=files, execution=execution,
            deploy_path=deploy_path, target=target, plan=plan,
            best_judge=best_judge, elapsed_seconds=elapsed_seconds
        )

        # 2. project note (aggregated)
        vault_write_project_note(project_name, run_id)

        # 3. model note
        if winning_model:
            vault_write_model_note(winning_model, run_id)

        # 4. target note
        vault_write_target_note(target, run_id)

        # 5. error pattern (if failed)
        if not success:
            stderr = execution.get("stderr", "")
            error_key = _classify_error(stderr)
            vault_write_error_note(
                error_key=error_key,
                error_summary=stderr[:300],
                language=language,
                project_name=project_name,
                model=winning_model,
                stage="execution" if troubleshoot_attempts == 0 else "troubleshoot",
                run_id=run_id
            )

        # 6. daily digest
        vault_write_daily_digest(run_id)

        # 7. index
        vault_write_index(run_id)

        # sync to NAS mirror
        if VAULT_NAS_ENABLED:
            try:
                vault_sync_to_nas(run_id)
            except (subprocess.SubprocessError, OSError) as e:
                log(run_id, f"vault-nas: sync failed (non-fatal): {e}")

        log(run_id, "vault: notes written successfully")

    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log(run_id, f"vault: write failed (non-fatal): {e}")


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


# ------------------------------------------------
# LANGUAGE HANDLERS
# ------------------------------------------------

# -- Verification: local-first with SSH fallback --

def verify_local(code, command_args, tmp_path):
    """Try to verify locally. Raises FileNotFoundError if tool missing."""
    with open(tmp_path, "w") as f:
        f.write(code)
    r = subprocess.run(command_args, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, r.stderr
    return True, ""


def verify_remote(code, target, remote_cmd, run_id):
    """Verify on the target Pi via SSH."""
    remote_tmp = "/tmp/ai_verify_tmp"
    fd, local_tmp = tempfile.mkstemp(prefix="ai_verify_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        try:
            deploy_file(local_tmp, remote_tmp, target)
        except RuntimeError as e:
            return False, f"Could not deploy for verification: {e}"
        result = ssh_command(target, remote_cmd.replace("{file}", remote_tmp))
        if result["returncode"] != 0:
            return False, result["stderr"]
        return True, ""
    finally:
        try:
            os.remove(local_tmp)
        except OSError:
            pass


VERIFY_CONFIG = {
    "python": {
        "local_args": lambda p: ["python3", "-m", "py_compile", p],
        "local_tmp": "/tmp/verify_script.py",
        "remote_cmd": "python3 -m py_compile {file}",
    },
    "bash": {
        "local_args": lambda p: ["bash", "-n", p],
        "local_tmp": "/tmp/verify_script.sh",
        "remote_cmd": "bash -n {file}",
    },
    "javascript": {
        "local_args": lambda p: ["node", "--check", p],
        "local_tmp": "/tmp/verify_script.js",
        "remote_cmd": "node --check {file}",
    },
}


def verify_code(code, language, target, run_id):
    """Verify a single file. Tries local first, falls back to SSH."""
    lang_key = language.lower().strip()
    # resolve aliases to primary names
    alias_map = {"node": "javascript", "nodejs": "javascript",
                 "node.js": "javascript", "js": "javascript",
                 "sh": "bash", "shell": "bash"}
    lang_key = alias_map.get(lang_key, lang_key)
    if lang_key not in VERIFY_CONFIG:
        lang_key = "python"

    cfg = VERIFY_CONFIG[lang_key]
    tmp_path = cfg["local_tmp"]

    # try local
    try:
        return verify_local(code, cfg["local_args"](tmp_path), tmp_path)
    except FileNotFoundError:
        pass

    # fallback: SSH to target
    if target:
        return verify_remote(code, target, cfg["remote_cmd"], run_id)

    return False, f"Cannot verify {language}: tool not installed locally and no target"


# -- Python dependency detection --

PYTHON_STDLIB = {
    "sys", "os", "math", "json", "time", "random", "subprocess", "threading",
    "multiprocessing", "urllib", "http", "pathlib", "itertools", "collections",
    "functools", "re", "datetime", "hashlib", "base64", "socket", "shutil",
    "tempfile", "argparse", "logging", "asyncio", "typing", "dataclasses",
    "enum", "abc", "io", "struct", "csv", "sqlite3", "unittest", "textwrap",
    "string", "copy", "pprint", "traceback", "signal", "ctypes", "array",
    "bisect", "heapq", "operator", "contextlib", "weakref", "inspect",
    "importlib", "pkgutil", "zipfile", "tarfile", "gzip", "bz2", "lzma",
    "configparser", "secrets", "hmac", "ssl", "select", "selectors",
    "statistics", "decimal", "fractions", "numbers", "cmath", "glob",
    "fnmatch", "xml", "html", "email", "mailbox", "mimetypes", "codecs",
    "unicodedata", "locale", "gettext", "platform", "errno", "warnings",
    "atexit", "sched", "queue", "concurrent", "venv", "distutils",
    "compileall", "dis", "token", "tokenize", "symtable", "keyword"
}


def detect_python_deps(code):

    packages = set()

    try:
        tree = ast.parse(code)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    pkg = name.name.split(".")[0]
                    if pkg not in PYTHON_STDLIB:
                        packages.add(pkg)

            if isinstance(node, ast.ImportFrom):
                if node.module:
                    pkg = node.module.split(".")[0]
                    if pkg not in PYTHON_STDLIB:
                        packages.add(pkg)

    except SyntaxError as e:
        print(f"python dependency detection: could not parse code: {e}")

    return packages


# -- JavaScript dependency detection --

def detect_node_deps(code):
    """Detect npm require() and import calls."""

    NODE_BUILTINS = {
        "fs", "path", "os", "http", "https", "url", "util", "events",
        "stream", "buffer", "crypto", "zlib", "child_process", "cluster",
        "net", "dns", "tls", "readline", "repl", "vm", "assert",
        "querystring", "string_decoder", "timers", "console", "process",
        "worker_threads", "perf_hooks", "async_hooks", "inspector",
        "node:fs", "node:path", "node:os", "node:http", "node:https",
        "node:url", "node:util", "node:events", "node:stream",
        "node:crypto", "node:child_process", "node:net"
    }

    packages = set()

    # require('package') or require("package")
    for match in re.findall(r"""require\s*\(\s*['"]([^'"./][^'"]*)['"]\s*\)""", code):
        pkg = match.split("/")[0]
        if pkg not in NODE_BUILTINS:
            packages.add(pkg)

    # import ... from 'package' or import 'package'
    for match in re.findall(r"""(?:from|import)\s+['"]([^'"./][^'"]*)['"]\s*""", code):
        pkg = match.split("/")[0]
        if pkg not in NODE_BUILTINS:
            packages.add(pkg)

    return packages


# -- Bash dependency detection --

def detect_bash_deps(code):
    """Bash scripts don't have pip-style deps. Return empty."""
    return set()


# -- Language handler registry --

LANG_HANDLERS = {
    "python": {
        "detect_deps": detect_python_deps,
        "file_extensions": {".py"},
        "default_entrypoint": "main.py",
        "run_command": "python3",
        "env_type": "venv",
        "dep_installer": "pip",
    },
    "bash": {
        "detect_deps": detect_bash_deps,
        "file_extensions": {".sh"},
        "default_entrypoint": "main.sh",
        "run_command": "bash",
        "env_type": "none",
        "dep_installer": "none",
    },
    "javascript": {
        "detect_deps": detect_node_deps,
        "file_extensions": {".js", ".mjs"},
        "default_entrypoint": "index.js",
        "run_command": "node",
        "env_type": "npm",
        "dep_installer": "npm",
    },
    "node": None,       # alias
    "nodejs": None,     # alias
    "node.js": None,    # alias
    "js": None,         # alias
    "sh": None,         # alias
    "shell": None,      # alias
}

# resolve aliases
LANG_HANDLERS["node"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["nodejs"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["node.js"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["js"] = LANG_HANDLERS["javascript"]
LANG_HANDLERS["sh"] = LANG_HANDLERS["bash"]
LANG_HANDLERS["shell"] = LANG_HANDLERS["bash"]


def get_lang_handler(language):
    """Get the handler for a language. Falls back to python."""
    lang = language.lower().strip() if language else "python"
    handler = LANG_HANDLERS.get(lang)
    if handler is None:
        return LANG_HANDLERS["python"]
    return handler


def get_verifiable_extensions(language):
    """Get file extensions that should be verified for a language."""
    handler = get_lang_handler(language)
    return handler["file_extensions"]


# ------------------------------------------------
# MULTI-LANGUAGE VERIFICATION
# ------------------------------------------------

def verify_files(files, language, target, run_id):
    """Verify all source files. Tries local, falls back to SSH on target."""

    handler = get_lang_handler(language)
    extensions = handler["file_extensions"]

    log(run_id, f"verifying {len(files)} file(s) [{language}]")

    errors = {}
    all_passed = True

    for filename, content in files.items():

        ext = os.path.splitext(filename)[1]

        if ext not in extensions:
            continue

        passed, error = verify_code(content, language, target, run_id)

        if not passed:
            log(run_id, f"verification failed: {filename}: {error[:200]}")
            errors[filename] = error
            all_passed = False
        else:
            log(run_id, f"verification passed: {filename}")

    return all_passed, errors


# ------------------------------------------------
# MULTI-LANGUAGE DEPENDENCY DETECTION
# ------------------------------------------------

def detect_all_dependencies(files, language, project_modules):
    """Detect dependencies across all files using the right language handler."""

    handler = get_lang_handler(language)
    detect_fn = handler["detect_deps"]
    extensions = handler["file_extensions"]

    all_deps = set()

    for filename, content in files.items():
        ext = os.path.splitext(filename)[1]
        if ext in extensions:
            deps = detect_fn(content)
            all_deps.update(deps)

    # remove local project module names
    all_deps -= project_modules

    return list(all_deps)


def get_project_modules(files):

    modules = set()

    for filename in files:
        # handle .py modules
        if filename.endswith(".py"):
            base = filename.replace(".py", "").replace("/", ".")
            top_level = base.split(".")[0]
            modules.add(top_level)
        # handle .js local modules
        elif filename.endswith(".js"):
            base = filename.replace(".js", "").replace("/", ".")
            top_level = base.split(".")[0]
            modules.add(top_level)
            modules.add(f"./{filename}")
            modules.add(f"./{filename.replace('.js', '')}")

    return modules


# ------------------------------------------------
# SANITIZE PACKAGE NAMES
# ------------------------------------------------

def sanitize_packages(packages):
    safe = []
    for pkg in packages:
        pkg = pkg.strip()
        if SAFE_PKG_PATTERN.match(pkg) and len(pkg) < 100:
            safe.append(pkg)
        else:
            print(f"WARNING: rejected suspicious package name: {repr(pkg)}")
    return safe


# ------------------------------------------------
# SSH
# ------------------------------------------------

def validate_target(target):
    if target not in SSH_TARGETS:
        raise ValueError(
            f"Unknown deploy target '{target}'. "
            f"Available: {list(SSH_TARGETS.keys())}"
        )


def ssh_command(target, command, *, quote_command=False):

    validate_target(target)

    cfg = SSH_TARGETS[target]

    # quote_command=True for user-supplied input; False for internal shell pipelines
    remote_cmd = shlex.quote(command) if quote_command else command

    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={SSH_TIMEOUT}",
        "-i",
        cfg["key_path"],
        f"{cfg['username']}@{cfg['host']}",
        remote_cmd
    ]

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT + 30
        )

        return {
            "stdout": r.stdout,
            "stderr": r.stderr,
            "returncode": r.returncode
        }

    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"SSH command timed out after {SSH_TIMEOUT + 30}s",
            "returncode": -1
        }


def deploy_file(local, remote, target):

    validate_target(target)

    cfg = SSH_TARGETS[target]

    r = subprocess.run(
        [
            "scp",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={SSH_TIMEOUT}",
            "-i",
            cfg["key_path"],
            local,
            f"{cfg['username']}@{cfg['host']}:{remote}"
        ],
        capture_output=True,
        text=True,
        timeout=SSH_TIMEOUT + 30
    )

    if r.returncode != 0:
        raise RuntimeError(f"SCP failed ({r.returncode}): {r.stderr.strip()}")


def deploy_project(files, target, remote_dir, run_id):
    """Deploy files to a specific directory on the target."""

    log(run_id, f"deploying {len(files)} file(s) to {target}:{remote_dir}")

    q_dir = shlex.quote(remote_dir)
    ssh_command(target, f"rm -rf {q_dir} && mkdir -p {q_dir}")

    for filename, content in files.items():

        remote_path = f"{remote_dir}/{filename}"
        file_remote_dir = os.path.dirname(remote_path)

        if file_remote_dir and file_remote_dir != remote_dir:
            ssh_command(target, f"mkdir -p {shlex.quote(file_remote_dir)}")

        fd, local_path = tempfile.mkstemp(prefix="ai_deploy_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            deploy_file(local_path, remote_path, target)
            log(run_id, f"deployed: {filename}")
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass

    log(run_id, "all files deployed")


# ------------------------------------------------
# ENVIRONMENT INSPECTOR
# ------------------------------------------------

def environment_inspector(target, run_id):

    log(run_id, "deep environment inspection")

    cmds = {
        "os": "cat /etc/os-release",
        "arch": "uname -m",
        "kernel": "uname -r",
        "python": "python3 --version",
        "pip_packages": "python3 -m pip list --format=json",
        "node": "node --version",
        "npm": "npm --version",
        "memory": "free -h",
        "disk": "df -h",
        "cpu": "nproc",
        "docker": "docker --version",
        "usb": "lsusb"
    }

    env = {}

    for k, c in cmds.items():
        r = ssh_command(target, c)
        env[k] = r["stdout"].strip()

    return env


# ------------------------------------------------
# VENV MANAGEMENT
# ------------------------------------------------

def ensure_venv(target, venv_path, run_id):
    """Create venv on target only if it doesn't already exist."""

    check = ssh_command(target, f"test -f {venv_path}/bin/python3 && echo EXISTS")

    if "EXISTS" in check["stdout"]:
        log(run_id, f"reusing existing venv: {venv_path}")
        return

    log(run_id, f"creating new venv: {venv_path}")
    result = ssh_command(target, f"python3 -m venv {venv_path}")

    if result["returncode"] != 0:
        log(run_id, f"venv creation failed: {result['stderr'][:300]}")


def install_dependencies(target, deps, venv_path, run_id):
    """Install dependencies into a specific venv. Returns True if successful."""

    if not deps:
        return True

    dep_str = " ".join(deps)

    log(run_id, f"installing dependencies: {deps}")

    install_result = ssh_command(
        target,
        f"source {venv_path}/bin/activate && pip install --disable-pip-version-check {dep_str}"
    )

    if install_result["returncode"] != 0:
        log(run_id, f"pip install failed: {install_result['stderr'][:500]}")
        return False

    log(run_id, "dependencies installed successfully")
    return True


# ------------------------------------------------
# SANDBOX EXECUTION (uses /tmp, disposable)
# ------------------------------------------------

def sandbox_execute(target, files, entrypoint, language, plan, run_id):
    """Deploy and test-run in the sandbox. Language and project-type aware."""

    handler = get_lang_handler(language)
    env_type = handler["env_type"]
    run_command = handler["run_command"]

    project_type = plan.get("project_type", "script")
    port = plan.get("port", 0)

    # check system-level dependencies (auto-install if sudo enabled)
    missing = ensure_system_dependencies(target, language, plan, run_id)
    if missing:
        log(run_id, f"WARNING: missing system deps {missing}, execution may fail")

    project_modules = get_project_modules(files)
    deps = detect_all_dependencies(files, language, project_modules)
    deps = sanitize_packages(deps)

    # set up environment based on language
    if env_type == "venv":
        ensure_venv(target, SANDBOX_VENV, run_id)
        pip_ok = install_dependencies(target, deps, SANDBOX_VENV, run_id)
        if not pip_ok and deps:
            log(run_id, "WARNING: proceeding despite pip failure")

    elif env_type == "npm" and deps:
        deploy_project(files, target, SANDBOX_DIR, run_id)
        pkg_json = json.dumps({
            "name": "ai-sandbox",
            "version": "1.0.0",
            "dependencies": {d: "*" for d in deps}
        }, indent=2)
        fd, local_pkg = tempfile.mkstemp(prefix="ai_sandbox_pkg_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pkg_json)
            deploy_file(local_pkg, f"{SANDBOX_DIR}/package.json", target)
        finally:
            try:
                os.remove(local_pkg)
            except OSError:
                pass
        log(run_id, f"npm installing: {deps}")
        npm_result = ssh_command(target, f"cd {SANDBOX_DIR} && npm install --no-audit --no-fund")
        if npm_result["returncode"] != 0:
            log(run_id, f"npm install failed: {npm_result['stderr'][:300]} {npm_result['stdout'][:200]}")

    # deploy files (skip if already deployed for npm)
    if env_type != "npm" or not deps:
        deploy_project(files, target, SANDBOX_DIR, run_id)

    # build the run command prefix
    if env_type == "venv":
        cmd_prefix = f"source {SANDBOX_VENV}/bin/activate && cd {SANDBOX_DIR}"
    else:
        cmd_prefix = f"cd {SANDBOX_DIR}"

    # --- SERVER EXECUTION ---
    if project_type == "server" and port > 0:
        return sandbox_execute_server(
            target, run_command, entrypoint, cmd_prefix, port, files, run_id
        )

    # --- SCRIPT EXECUTION ---
    log(run_id, f"executing entrypoint: {run_command} {entrypoint}")

    run_cmd = f"{cmd_prefix} && {run_command} {entrypoint}"

    return ssh_command(target, run_cmd)


# ------------------------------------------------
# PORT AWARENESS
# ------------------------------------------------

def check_port_available(target, port, run_id):
    """Check if a port is available on the target."""
    result = ssh_command(target, f"ss -tlnp 2>/dev/null | grep ':{port} ' || echo FREE")
    return "FREE" in result["stdout"]


def find_available_port(target, preferred_port, run_id):
    """
    Check if the preferred port is free. If not, scan nearby ports.
    Returns (port, was_changed).
    """

    if check_port_available(target, preferred_port, run_id):
        return preferred_port, False

    log(run_id, f"port {preferred_port} is in use, scanning for alternatives")

    # try nearby ports: preferred+1 through preferred+20
    for offset in range(1, 21):
        candidate = preferred_port + offset
        if candidate > 65535:
            break
        if check_port_available(target, candidate, run_id):
            log(run_id, f"found available port: {candidate}")
            return candidate, True

    # try common alternative ports
    for candidate in [8080, 8000, 8888, 9000, 5000, 4000]:
        if candidate != preferred_port and check_port_available(target, candidate, run_id):
            log(run_id, f"found available port: {candidate}")
            return candidate, True

    log(run_id, f"WARNING: could not find available port near {preferred_port}, using it anyway")
    return preferred_port, False


def patch_port_in_files(files, old_port, new_port):
    """Replace port references in generated code when we need to use a different port."""

    patched = {}

    for filename, content in files.items():
        patched[filename] = content.replace(str(old_port), str(new_port))

    return patched


# ------------------------------------------------
# SYSTEM DEPENDENCY INSTALLER
# ------------------------------------------------

# sudo allowlist: only these commands can be run with sudo
SUDO_ALLOWED = CONFIG.get("sudo", {}).get("allowed_commands", [
    "apt-get install -y",
    "apt-get update",
    "npm install -g",
    "systemctl start",
    "systemctl stop",
    "systemctl restart",
    "systemctl enable",
    "systemctl status",
])

SUDO_ENABLED = CONFIG.get("sudo", {}).get("enabled", False)


def sudo_command(target, command, run_id):
    """
    Run a command with sudo on the target, but only if:
    1. Sudo is enabled in config
    2. The command matches the allowlist
    """

    if not SUDO_ENABLED:
        log(run_id, f"sudo disabled, skipping: {command}")
        return ssh_command(target, command)

    # check against allowlist
    allowed = False
    for pattern in SUDO_ALLOWED:
        if command.strip().startswith(pattern):
            allowed = True
            break

    if not allowed:
        log(run_id, f"sudo command not in allowlist, running without sudo: {command}")
        return ssh_command(target, command)

    log(run_id, f"sudo: {command}")
    return ssh_command(target, f"sudo {command}")


# map of language -> required system packages
SYSTEM_DEPS = {
    "python": {
        "checks": ["python3 --version"],
        "packages_apt": ["python3", "python3-venv", "python3-pip"],
    },
    "bash": {
        "checks": ["bash --version"],
        "packages_apt": ["bash"],
    },
    "javascript": {
        "checks": ["node --version", "npm --version"],
        "packages_apt": ["nodejs", "npm"],
    },
}


def ensure_system_dependencies(target, language, plan, run_id):
    """
    Check that the target has the required system-level tools for the language.
    Auto-installs via apt if sudo is enabled and tools are missing.
    Returns list of missing tools (empty if all good).
    """

    handler = get_lang_handler(language)
    lang_key = None
    for key in SYSTEM_DEPS:
        if get_lang_handler(key) == handler:
            lang_key = key
            break

    if not lang_key or lang_key not in SYSTEM_DEPS:
        return []

    dep_info = SYSTEM_DEPS[lang_key]

    # check all required tools
    all_present = True
    for check_cmd in dep_info["checks"]:
        check_result = ssh_command(target, f"{check_cmd} 2>/dev/null")
        if check_result["returncode"] != 0:
            log(run_id, f"system dependency missing: '{check_cmd}' failed on {target}")
            all_present = False

    if all_present:
        return []

    if not SUDO_ENABLED:
        log(run_id, f"sudo not enabled, cannot auto-install. Missing: {dep_info['packages_apt']}")
        return dep_info["packages_apt"]

    log(run_id, f"auto-installing system dependencies: {dep_info['packages_apt']}")

    sudo_command(target, "apt-get update", run_id)

    pkg_str = " ".join(dep_info["packages_apt"])
    install_result = sudo_command(target, f"apt-get install -y {pkg_str}", run_id)

    if install_result["returncode"] != 0:
        log(run_id, f"apt install failed: {install_result['stderr'][:300]}")
        return dep_info["packages_apt"]

    log(run_id, "system dependencies installed successfully")
    return []


# ------------------------------------------------
# SERVER-AWARE SANDBOX EXECUTION
# ------------------------------------------------

def sandbox_execute_server(target, run_command, entrypoint, cmd_prefix, port, files, run_id):
    """
    Execute a server in the sandbox:
    1. Check port availability, find alternative if needed
    2. Kill anything on the chosen port
    3. Start the server in the background
    4. Wait for the port to become available
    5. Curl the server to test it
    6. Kill the server
    7. Return the curl result as execution output
    """

    # port awareness: find an available port
    actual_port, port_changed = find_available_port(target, port, run_id)

    if port_changed:
        log(run_id, f"port {port} in use, switched to {actual_port}")
        # patch port references in the deployed files
        patched_files = patch_port_in_files(files, port, actual_port)
        deploy_project(patched_files, target, SANDBOX_DIR, run_id)

    log(run_id, f"executing server: {run_command} {entrypoint} (port {actual_port})")

    # kill any leftover process on this port
    ssh_command(target, f"fuser -k {int(actual_port)}/tcp 2>/dev/null || true")

    time.sleep(1)

    # start server in background, capture PID
    q_entry = shlex.quote(entrypoint)
    start_cmd = (
        f"{cmd_prefix} && "
        f"nohup {run_command} {q_entry} < /dev/null > /tmp/ai_server.log 2>&1 & "
        f"echo $!"
    )

    start_result = ssh_command(target, start_cmd)
    server_pid = start_result["stdout"].strip().splitlines()[-1] if start_result["stdout"] else ""

    if not server_pid or not server_pid.isdigit():
        log(run_id, f"failed to start server, could not capture PID: {start_result['stderr']}")
        return {
            "stdout": start_result["stdout"],
            "stderr": f"Failed to start server: {start_result['stderr']}",
            "returncode": 1
        }

    log(run_id, f"server started with PID {server_pid}")

    # wait for port to become available (up to 15 seconds)
    port_ready = False
    for attempt in range(15):
        time.sleep(1)
        check = ssh_command(target, f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{actual_port}/ 2>/dev/null || echo FAIL")
        response = check["stdout"].strip()
        if response and response != "FAIL" and response != "000":
            port_ready = True
            log(run_id, f"server responding on port {actual_port} (attempt {attempt + 1})")
            break

    if not port_ready:
        alive = ssh_command(target, f"kill -0 {server_pid} 2>/dev/null && echo ALIVE || echo DEAD")
        server_log = ssh_command(target, "cat /tmp/ai_server.log 2>/dev/null")

        ssh_command(target, f"kill {server_pid} 2>/dev/null || true")
        ssh_command(target, f"fuser -k {actual_port}/tcp 2>/dev/null || true")

        log(run_id, f"server did not respond on port {actual_port} within 15s (process: {alive['stdout'].strip()})")

        return {
            "stdout": "",
            "stderr": f"Server did not respond on port {actual_port}. "
                      f"Process: {alive['stdout'].strip()}. "
                      f"Log: {server_log['stdout'][:500]}",
            "returncode": 1
        }

    # server is up — test it
    log(run_id, f"testing server at http://localhost:{actual_port}/")

    test_result = ssh_command(target, f"curl -s --max-time 10 http://localhost:{actual_port}/")

    server_log = ssh_command(target, "cat /tmp/ai_server.log 2>/dev/null")

    # cleanup: kill the server
    ssh_command(target, f"kill {server_pid} 2>/dev/null || true")
    ssh_command(target, f"fuser -k {actual_port}/tcp 2>/dev/null || true")

    log(run_id, "server tested and stopped")

    return {
        "stdout": test_result["stdout"],
        "stderr": server_log["stdout"][:500] if server_log["stdout"] else "",
        "returncode": test_result["returncode"]
    }


# ------------------------------------------------
# PERSISTENT DEPLOYMENT
# ------------------------------------------------

def persistent_deploy(target, project_name, files, entrypoint, language, plan, deps,
                      run_id, score, prompt, run_id_str):
    """
    Promote a successful sandbox run to a persistent project directory.
    Language-aware: sets up the appropriate environment per language.
    """

    handler = get_lang_handler(language)
    env_type = handler["env_type"]
    run_command = handler["run_command"]

    # resolve the actual deploy base path on the target
    resolve = ssh_command(target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    if not base:
        base = "/tmp/ai-projects"
        log(run_id_str, f"WARNING: could not resolve deploy base, using {base}")

    project_dir = f"{base}/{project_name}"
    src_dir = f"{project_dir}/src"

    log(run_id_str, f"persistent deploy [{language}] -> {target}:{project_dir}")

    # create project structure
    ssh_command(target, f"mkdir -p {shlex.quote(src_dir)}")

    # deploy source files
    for filename, content in files.items():

        remote_path = f"{src_dir}/{filename}"
        file_dir = os.path.dirname(remote_path)

        if file_dir and file_dir != src_dir:
            ssh_command(target, f"mkdir -p {shlex.quote(file_dir)}")

        fd, local_path = tempfile.mkstemp(prefix="ai_persist_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            deploy_file(local_path, remote_path, target)
            log(run_id_str, f"persisted: {filename}")
        finally:
            try:
                os.remove(local_path)
            except OSError:
                pass

    deps_sanitized = sanitize_packages(deps)

    # set up environment based on language
    if env_type == "venv":
        venv_dir = f"{project_dir}/.venv"
        ensure_venv(target, venv_dir, run_id_str)
        if deps_sanitized:
            install_dependencies(target, deps_sanitized, venv_dir, run_id_str)

    elif env_type == "npm" and deps_sanitized:
        pkg_json = json.dumps({
            "name": project_name,
            "version": "1.0.0",
            "dependencies": {d: "*" for d in deps_sanitized}
        }, indent=2)
        fd, local_pkg = tempfile.mkstemp(prefix="ai_persist_pkg_", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pkg_json)
            deploy_file(local_pkg, f"{src_dir}/package.json", target)
        finally:
            try:
                os.remove(local_pkg)
            except OSError:
                pass
        log(run_id_str, f"npm installing: {deps_sanitized}")
        ssh_command(target, f"cd {src_dir} && npm install --no-audit --no-fund")

    # generate language-specific run.sh
    if env_type == "venv":
        run_script = f"""#!/bin/bash
# Auto-generated by AI Orchestrator
# Project: {project_name} | Language: {language}
# Run ID:  {run_id} | Score: {score}

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"
cd "$SCRIPT_DIR/src"
{run_command} {entrypoint} "$@"
"""
    else:
        run_script = f"""#!/bin/bash
# Auto-generated by AI Orchestrator
# Project: {project_name} | Language: {language}
# Run ID:  {run_id} | Score: {score}

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
cd "$SCRIPT_DIR/src"
{run_command} {entrypoint} "$@"
"""

    fd, local_run = tempfile.mkstemp(prefix="ai_persist_run_", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(run_script)
        deploy_file(local_run, f"{project_dir}/run.sh", target)
    finally:
        try:
            os.remove(local_run)
        except OSError:
            pass
    ssh_command(target, f"chmod +x {shlex.quote(project_dir)}/run.sh")

    # make bash/shell entrypoints executable too
    if language.lower() in ("bash", "sh", "shell"):
        ssh_command(target, f"chmod +x {shlex.quote(src_dir)}/{shlex.quote(entrypoint)}")

    # create project.json metadata
    metadata = {
        "project_name": project_name,
        "language": language,
        "run_id": run_id,
        "score": score,
        "prompt": prompt,
        "entrypoint": entrypoint,
        "files": list(files.keys()),
        "dependencies": deps_sanitized,
        "target": target,
        "deploy_path": project_dir,
        "deployed_at": datetime.utcnow().isoformat(),
        "plan": plan
    }

    local_meta = "/tmp/ai_persist_meta.json"
    with open(local_meta, "w") as f:
        json.dump(metadata, f, indent=2)

    deploy_file(local_meta, f"{project_dir}/project.json", target)

    log(run_id_str, f"persistent deploy complete: {project_dir}")

    return project_dir


# ------------------------------------------------
# TOOL REGISTRY + DISPATCHER
# ------------------------------------------------

TOOL_REGISTRY_PATH = Path("/opt/ai-orchestrator/tool_registry.json")

def load_tool_registry():
    """Load tool definitions from tool_registry.json. Hot-reloadable."""
    try:
        with open(TOOL_REGISTRY_PATH) as f:
            return json.load(f).get("tools", [])
    except (json.JSONDecodeError, OSError) as e:
        print(f"[tool_registry] failed to load: {e}")
        return []


def _save_tool_registry(tools: list):
    with open(TOOL_REGISTRY_PATH, "w") as f:
        json.dump({"tools": tools}, f, indent=2)


# destructive command patterns blocked from tool execution
_TOOL_CMD_BLOCKLIST = re.compile(
    r"rm\s+-rf\s+/(?!\btmp\b)|"       # rm -rf / (except /tmp)
    r"mkfs\.|"                          # formatting filesystems
    r"dd\s+.*of=/dev/|"                # raw disk writes
    r":\(\)\{.*\}|"                     # fork bomb
    r">\s*/dev/sd|"                     # overwrite block devices
    r"chmod\s+-R\s+777\s+/|"           # recursive 777 on root
    r"curl\s+.*\|\s*(?:ba)?sh|"        # pipe-to-shell
    r"wget\s+.*\|\s*(?:ba)?sh",        # pipe-to-shell
    re.IGNORECASE
)


def _sanitize_tool_args(args: dict, tool_name: str) -> dict:
    """Shell-quote tool arguments to prevent injection via template parameters.
    The run_command tool is exempt since its template IS the raw command."""
    if tool_name == "run_command":
        return {k: str(v) for k, v in (args or {}).items()}
    return {k: shlex.quote(str(v)) for k, v in (args or {}).items()}


def execute_tool(tool_name, args, target, run_id):
    """Execute a single tool against the target. Returns stdout string."""

    registry = load_tool_registry()
    defn = next((t for t in registry if t["name"] == tool_name), None)

    if not defn:
        return f"[error] unknown tool: {tool_name}"

    handler = defn.get("handler", "builtin")

    if handler == "builtin":
        cmd_template = defn.get("command", "")
        try:
            safe_args = _sanitize_tool_args(args, tool_name)
            cmd = cmd_template.format(**safe_args)
        except KeyError as e:
            return f"[error] missing argument {e} for tool {tool_name}"

        # Gates: check learned safety rules before execution
        allowed, gate, gate_msg = check_gate(cmd, tool_name, args, run_id)
        if not allowed:
            log(run_id, f"GATE BLOCKED: {gate_msg}")
            return f"[error] blocked by gate: {gate.get('reason', 'safety rule')}"
        if gate and gate_msg:
            # warn-level gate matched — log but continue
            log(run_id, f"GATE WARN: {gate_msg}")

        # block destructive commands regardless of tool
        if _TOOL_CMD_BLOCKLIST.search(cmd):
            log(run_id, f"BLOCKED destructive tool command: {cmd[:200]}")
            record_lesson(cmd, "Blocked by hardcoded safety filter", {"tool": tool_name}, run_id, "system")
            return "[error] command blocked by safety filter"

        if target:
            result = ssh_command(target, cmd)
            out = result["stdout"].strip()
            err = result["stderr"].strip()
            if result["returncode"] != 0 and not out:
                err_msg = err[:300] if err else "command returned no output"
                record_runtime_failure(cmd, err_msg, tool_name, run_id)
                return f"[error] {err_msg}"
            return out or err or "[no output]"
        else:
            # local execution (no SSH target)
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                output = r.stdout.strip() or r.stderr.strip() or "[no output]"
                if r.returncode != 0:
                    record_runtime_failure(cmd, output[:300], tool_name, run_id)
                return output
            except Exception as e:
                record_runtime_failure(cmd, str(e), tool_name, run_id)
                return f"[error] {e}"

    elif handler == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return "[error] write_file requires 'path'"

        local_tmp = f"/tmp/ai_tool_write_{uuid.uuid4().hex[:8]}"
        try:
            with open(local_tmp, "w") as f:
                f.write(content)
            deploy_file(local_tmp, path, target)
            return f"written: {path}"
        except Exception as e:
            return f"[error] {e}"
        finally:
            try:
                os.remove(local_tmp)
            except OSError:
                pass

    return f"[error] unknown handler: {handler}"


def run_tools(prompt, plan, env, target, planner_model, run_id):
    """
    Full tool phase: dispatch → execute → return formatted context string.
    Returns empty string if no tools run.
    """

    registry = load_tool_registry()
    if not registry:
        return ""

    tool_descriptions = "\n".join(
        f"- {t['name']}: {t['description']}"
        + (f"\n  args: {json.dumps(t['parameters'])}" if t.get('parameters') else "")
        for t in registry
    )

    # Load tool_dispatch agent config from agents/tool_dispatch/
    td_cfg = load_agent("tool_dispatch")

    system_prompt = td_cfg.render_system_prompt()

    user_prompt = td_cfg.render_user_prompt(
        prompt=prompt,
        language=plan.get("language"),
        file_list=str(list(plan.get("files", {}).keys())),
        dependencies=str(plan.get("dependencies", [])),
        os=env.get("os", "Linux"),
        arch=env.get("arch", "unknown"),
        tool_descriptions=tool_descriptions,
    )

    result = query_ollama_structured(
        planner_model,
        system_prompt,
        user_prompt,
        TOOL_DISPATCH_SCHEMA,
        resolve_chat_url(planner_model),
        run_id
    )

    if not result or not isinstance(result, dict):
        log(run_id, "tool dispatch failed, skipping tools")
        return ""

    tools = result.get("tools", [])

    if not tools:
        log(run_id, "tool dispatch: no tools needed")
        return ""

    names = [t.get("name") for t in tools]
    log(run_id, f"tool dispatch: {names}")

    results = []
    for t in tools:
        name = t.get("name", "")
        args = t.get("args", {})
        if not name:
            continue
        log(run_id, f"tool: {name}" + (f" {args}" if args else ""))
        output = execute_tool(name, args, target, run_id)
        results.append(f"[{name}]\n{output}")

    if not results:
        return ""

    return "TOOL RESULTS (gathered before generation):\n" + "\n\n".join(results)


# ------------------------------------------------
# PLANNER (structured output)
# ------------------------------------------------

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
        run_id
    )

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

        log(run_id, f"planner: {lang} {ptype}, {len(result['files'])} file(s), entrypoint={result['entrypoint']}, mode={emode}" +
            (f", port={port}" if ptype == "server" else ""))
        return result

    # fallback: use /api/generate without schema enforcement
    log(run_id, "structured planner failed, falling back to unstructured")

    raw = query_ollama(model, user_prompt, OLLAMA_PLANNER, run_id)

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
        "steps": []
    }


# ------------------------------------------------
# JUDGE (structured output)
# ------------------------------------------------

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
            run_id
        )

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

        raw = query_ollama(model, user_prompt, OLLAMA_JUDGE, run_id)

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
            run_id
        )

        if result and isinstance(result, dict):
            overall = result.get("overall", 0)
            if isinstance(overall, (int, float)):
                overall = max(0, min(10, overall))
            else:
                overall = 0
            log(run_id, f"fallback judge score: {overall}")
            return overall, result

        raw = query_ollama(JUDGE_FALLBACK_MODEL, user_prompt, resolve_generate_url(JUDGE_FALLBACK_MODEL), run_id)
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

    raw = query_ollama(model, gen_prompt, resolve_generate_url(model), run_id)

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

    score, judge = judge_score(files, prompt, plan, judge_model, run_id)

    return {
        "model": model,
        "files": files,
        "score": score,
        "judge": judge
    }


# ------------------------------------------------
# OPTIMIZER
# ------------------------------------------------

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
        raw = query_ollama(model, p, resolve_generate_url(model), run_id)
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
        raw = query_ollama(model, p, resolve_generate_url(model), run_id)
        result = extract_files(raw, plan)
        return result if result else files


# ------------------------------------------------
# TROUBLESHOOTER
# ------------------------------------------------

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
        raw = query_ollama(model, p, resolve_generate_url(model), run_id)
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
        raw = query_ollama(model, p, resolve_generate_url(model), run_id)
        result = extract_files(raw, plan)
        return result if result else files


# ------------------------------------------------
# ORCHESTRATOR CORE
# ------------------------------------------------

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

        plan = planner_agent(req.prompt, req.planner_model, env, memory_context, run_id)

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

            with ThreadPoolExecutor(max_workers=len(req.generator_models)) as ex:

                # combine tool context and reference documents for generator
                combined_context = tool_context
                if ref_context:
                    combined_context = (combined_context + "\n" + ref_context) if combined_context else ref_context

                futures = {
                    ex.submit(
                        generate_candidate,
                        model,
                        req.prompt,
                        plan,
                        env,
                        req.judge_model,
                        req.deploy_target,
                        run_id,
                        combined_context
                    ): model
                    for model in req.generator_models
                }

                candidates = []
                for future in as_completed(futures):
                    try:
                        candidates.append(future.result())
                    except Exception as e:
                        model_name = futures[future]
                        log(run_id, f"generator failed for {model_name}: {e}")

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
                optimized_files = optimizer_agent(
                    best_files,
                    req.prompt,
                    best_judge,
                    plan,
                    req.optimizer_model,
                    run_id
                )

                if optimized_files and optimized_files != best_files:

                    all_passed, _ = verify_files(optimized_files, language, req.deploy_target, run_id)

                    if all_passed:

                        opt_score, opt_judge = judge_score(
                            optimized_files,
                            req.prompt,
                            plan,
                            req.judge_model,
                            run_id
                        )

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

            update_negative_memory(
                prompt=req.prompt,
                embedding=emb,
                run_id=run_id,
                project=req.project_name,
                language=language,
                error_summary="All generators failed to produce valid code across all iterations",
                failure_stage="generation",
                models_tried=models_tried,
                project_type=project_type
            )

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

            fixed_files = troubleshoot(
                best_files,
                execution["stderr"],
                req.prompt,
                plan,
                req.troubleshooter_model,
                run_id
            )

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
            update_memory(
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
            )
        else:
            # still record in positive memory (with success=False) for similarity matching
            update_memory(
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
            )

            # negative memory — record what went wrong
            error_summary = execution.get("stderr", "")[:500]
            if not error_summary:
                error_summary = f"Exit code {execution.get('returncode', '?')}"

            failure_stage = "execution"
            if troubleshoot_attempt >= MAX_TROUBLESHOOT_ATTEMPTS:
                failure_stage = "troubleshoot_exhausted"

            models_tried = list(set(c.get("model", "") for c in all_candidate_results if c.get("model")))

            update_negative_memory(
                prompt=req.prompt,
                embedding=emb,
                run_id=run_id,
                project=req.project_name,
                language=language,
                error_summary=error_summary,
                failure_stage=failure_stage,
                models_tried=models_tried,
                project_type=project_type
            )

        # rewrite primer.md with current state (Layer 2)
        env_summary = f"Target: {req.deploy_target}, Python: {env.get('python', '?')}, Node: {env.get('node', '?')}"
        try:
            rewrite_primer(
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
            )
        except Exception as e:
            log(run_id, f"primer rewrite failed (non-fatal): {e}")

        # record session (groups runs within time windows)
        try:
            session_id = record_session(
                run_id=run_id,
                project_name=req.project_name,
                prompt=req.prompt,
                language=language,
                score=best_score,
                success=execution["returncode"] == 0,
                winning_model=best_model,
                troubleshoot_attempts=troubleshoot_attempt
            )
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
                hindsight_retain(retain_content, run_id)
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
            vault_after_run(
                run_id=run_id, project_name=req.project_name, prompt=req.prompt,
                language=language, project_type=project_type, score=best_score,
                success=execution["returncode"] == 0, winning_model=best_model,
                troubleshoot_attempts=troubleshoot_attempt,
                entrypoint=entrypoint, files=best_files, execution=execution,
                deploy_path=deploy_path, target=req.deploy_target, plan=plan,
                best_judge=best_judge, elapsed_seconds=_run_elapsed_vault
            )
        except Exception as e:
            log(run_id, f"vault write failed (non-fatal): {e}")

        # Gates: consolidate lessons at end of run
        try:
            gate_report = consolidate_lessons(log_fn=log)
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
                available = set(_url_cache.keys()) if _url_cache else None
                dream_report = run_dream(available_models=available, log_fn=log)
                health = dream_report.get("health", {})
                log(run_id, f"dream: health={health.get('score', '?')}/100 ({health.get('rating', '?')})")
                _run_counter_since_dream = 0
            except Exception as e:
                log(run_id, f"dream auto-trigger failed (non-fatal): {e}")

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

@app.post("/orchestrate")
def orchestrate(req: OrchestrateRequest):

    if ORCHESTRATOR_PAUSED:
        raise HTTPException(status_code=503, detail="Orchestrator is paused")

    try:
        validate_target(req.deploy_target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = str(uuid.uuid4())

    _init_run_status(run_id, project=req.project_name, target=req.deploy_target)

    thread = threading.Thread(
        target=run_orchestration,
        args=(req, run_id),
        daemon=True
    )
    thread.start()

    return {
        "run_id": run_id,
        "status": "started",
        "poll": f"/status/{run_id}"
    }


@app.get("/status/{run_id}")
def status(run_id: str):

    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    info = RUN_STATUS[run_id]

    response = {
        "run_id": run_id,
        "phase": info["phase"],
        "score": info["score"],
        "completed": info["completed"],
    }

    if info.get("project"):
        response["project"] = info["project"]

    if info.get("target"):
        response["target"] = info["target"]

    if info["completed"]:

        if info["error"]:
            response["error"] = info["error"]

        if info["result"]:
            response["result"] = info["result"]

    return response


@app.get("/result/{run_id}")
def result(run_id: str):

    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    info = RUN_STATUS[run_id]

    if not info["completed"]:
        return {
            "run_id": run_id,
            "status": "running",
            "phase": info["phase"],
            "score": info["score"]
        }

    if info["error"]:
        raise HTTPException(status_code=500, detail=info["error"])

    return info["result"]


@app.get("/runs")
def list_runs():
    """List runs — combines in-memory active runs with persistent history from log files and run index."""

    # 1. Load persistent run index (fast, no SSH)
    run_index = _load_run_index()

    seen = set()
    runs = []

    # 2. In-memory active/recent runs (highest priority)
    for rid, info in RUN_STATUS.items():
        seen.add(rid)
        indexed = run_index.get(rid, {})
        runs.append({
            "run_id": rid,
            "phase": info["phase"],
            "score": info["score"] or indexed.get("score", 0),
            "completed": info["completed"],
            "project": info.get("project") or indexed.get("project", ""),
            "target": info.get("target") or indexed.get("target", ""),
            "has_error": info.get("error") is not None,
            "timestamp": indexed.get("timestamp"),
        })

    # 3. Runs from persistent index (survived restarts)
    for rid, indexed in run_index.items():
        if rid in seen:
            continue
        seen.add(rid)
        runs.append({
            "run_id": rid,
            "phase": indexed.get("phase", "completed"),
            "score": indexed.get("score", 0),
            "completed": True,
            "project": indexed.get("project", ""),
            "target": indexed.get("target", ""),
            "has_error": indexed.get("has_error", False),
            "timestamp": indexed.get("timestamp"),
        })

    # 4. Historical runs from log files (catch runs not yet in index)
    log_dir = Path(LOG_DIR)
    if log_dir.exists():
        log_files = sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        for lf in log_files[:100]:
            rid = lf.stem
            if rid in seen or rid == "graph-data":
                continue
            seen.add(rid)

            # Extract info from log content
            phase = "completed"
            has_error = False
            log_project = ""
            log_target = ""
            try:
                content = lf.read_text(errors="replace")
                all_lines = content.strip().splitlines()
                if all_lines:
                    last = all_lines[-1].lower()
                    if "error" in last or "crash" in last or "failed" in last:
                        phase = "failed"
                        has_error = True
                    for line in all_lines:
                        if "persistent deploy" in line and "->" in line:
                            m = re.search(r"->\s*(\S+):.*/([^/\s]+)\s*$", line)
                            if m:
                                log_target = m.group(1)
                                log_project = m.group(2)
                            break
            except OSError:
                pass

            ts = datetime.utcfromtimestamp(lf.stat().st_mtime).isoformat() if lf.exists() else None

            runs.append({
                "run_id": rid,
                "phase": phase,
                "score": 0,
                "completed": True,
                "project": log_project,
                "target": log_target,
                "has_error": has_error,
                "timestamp": ts,
            })

    # Sort: active (not completed) first, then by timestamp descending
    runs.sort(key=lambda r: (
        r.get("completed", True),
        r.get("timestamp") or "0000",
    ), reverse=True)

    return {"runs": runs}


@app.get("/files/{run_id}")
def get_files(run_id: str):

    if run_id not in RUN_STATUS:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")

    info = RUN_STATUS[run_id]

    if not info["completed"]:
        raise HTTPException(status_code=409, detail="Run not yet completed")

    project = info.get("project", "")

    files_path = Path(PROJECTS_DIR) / project / "runs" / run_id / "files.json"

    if not files_path.exists():
        raise HTTPException(status_code=404, detail="No files found for this run")

    try:
        files = json.loads(files_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Could not read files: {e}")

    return {
        "run_id": run_id,
        "project": project,
        "files": files
    }


@app.get("/environment/{target}")
def environment(target: str):

    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = "env-scan"

    return environment_inspector(target, run_id)


# ------------------------------------------------
# API: LIST DEPLOYED PROJECTS ON A TARGET
# ------------------------------------------------

@app.get("/deployed/{target}")
def list_deployed(target: str):
    """List all persistently deployed projects on a target."""

    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # resolve the deploy base
    resolve = ssh_command(target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    if not base:
        return {"target": target, "projects": []}

    # find all project.json files
    find_result = ssh_command(
        target,
        f"find {base} -maxdepth 2 -name 'project.json' -type f 2>/dev/null"
    )

    if find_result["returncode"] != 0 or not find_result["stdout"].strip():
        return {"target": target, "projects": []}

    projects = []

    for meta_path in find_result["stdout"].strip().splitlines():
        meta_path = meta_path.strip()
        if not meta_path:
            continue

        cat_result = ssh_command(target, f"cat {meta_path}")

        if cat_result["returncode"] != 0:
            continue

        try:
            meta = json.loads(cat_result["stdout"])
            projects.append(meta)
        except json.JSONDecodeError:
            continue

    # sort by deploy time, newest first
    projects.sort(key=lambda x: x.get("deployed_at", ""), reverse=True)

    return {"target": target, "projects": projects}


# ------------------------------------------------
# API: RUN A DEPLOYED PROJECT
# ------------------------------------------------

class RunDeployedRequest(BaseModel):

    project_name: str
    target: str
    args: str | None = None


@app.post("/run-deployed")
def run_deployed(req: RunDeployedRequest):
    """Execute an already-deployed project on the target."""

    try:
        validate_target(req.target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # resolve the deploy path
    resolve = ssh_command(req.target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    project_dir = f"{base}/{req.project_name}"

    # check that the project exists
    check = ssh_command(req.target, f"test -f {project_dir}/run.sh && echo EXISTS")

    if "EXISTS" not in check["stdout"]:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{req.project_name}' not found on {req.target}. "
                   f"Expected: {project_dir}/run.sh"
        )

    # execute it
    args_str = f" {shlex.quote(req.args)}" if req.args else ""

    try:
        execution = ssh_command(req.target, f"bash {shlex.quote(project_dir)}/run.sh{args_str}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SSH execution failed: {e}")

    return {
        "project": req.project_name,
        "target": req.target,
        "deploy_path": project_dir,
        "execution": execution
    }


# ------------------------------------------------
# API: DELETE A DEPLOYED PROJECT
# ------------------------------------------------

class DeleteDeployedRequest(BaseModel):

    project_name: str
    target: str


@app.post("/delete-deployed")
def delete_deployed(req: DeleteDeployedRequest):
    """Remove a deployed project from a target."""

    try:
        validate_target(req.target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    resolve = ssh_command(req.target, f"echo {DEPLOY_BASE}")
    base = resolve["stdout"].strip()

    project_dir = f"{base}/{req.project_name}"

    # safety check: make sure we're deleting inside the deploy base
    if not project_dir.startswith(base) or ".." in req.project_name:
        raise HTTPException(status_code=400, detail="Invalid project name")

    # check it exists
    check = ssh_command(req.target, f"test -d {shlex.quote(project_dir)} && echo EXISTS")

    if "EXISTS" not in check["stdout"]:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{req.project_name}' not found on {req.target}"
        )

    try:
        ssh_command(req.target, f"rm -rf {shlex.quote(project_dir)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SSH deletion failed: {e}")

    return {
        "deleted": req.project_name,
        "target": req.target,
        "path": project_dir
    }


# ------------------------------------------------
# API: MEMORY ENDPOINTS
# ------------------------------------------------

@app.get("/memory")
def get_memory():
    """View positive memory (past runs with similarity data)."""

    index = load_prompt_index()

    # return without embeddings (they're huge)
    entries = []
    for entry in index:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        entries.append(e)

    entries.reverse()  # newest first

    return {
        "total": len(entries),
        "entries": entries[:50]  # last 50
    }


@app.get("/memory/negative")
def get_negative_memory():
    """View negative memory (past failures)."""

    entries = load_negative_memory()

    # return without embeddings
    clean = []
    for entry in entries:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        clean.append(e)

    clean.reverse()  # newest first

    return {
        "total": len(clean),
        "entries": clean[:50]
    }


@app.get("/memory/search")
def search_memory(q: str):
    """Search memory by semantic similarity to a query string."""

    positive = find_similar(q)
    negative = find_negative_matches(q)

    pos_results = []
    for sim_score, entry in positive:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        e["similarity"] = round(sim_score, 4)
        pos_results.append(e)

    neg_results = []
    for sim_score, entry in negative:
        e = {k: v for k, v in entry.items() if k != "embedding"}
        e["similarity"] = round(sim_score, 4)
        neg_results.append(e)

    return {
        "query": q,
        "positive_matches": pos_results,
        "negative_matches": neg_results
    }


@app.get("/model-stats")
def get_model_stats_endpoint():
    """View model performance statistics."""

    stats = load_model_stats()

    # compute derived metrics
    summary = {}
    for model, s in stats.items():
        total = s["total_runs"]
        avg_score = s["total_score"] / total if total > 0 else 0
        win_rate = (s["wins"] / total * 100) if total > 0 else 0
        fail_rate = (s["failures"] / total * 100) if total > 0 else 0

        summary[model] = {
            "total_runs": total,
            "avg_score": round(avg_score, 2),
            "win_rate": round(win_rate, 1),
            "fail_rate": round(fail_rate, 1),
            "wins": s["wins"],
            "failures": s["failures"],
            "by_language": s["by_language"],
            "by_role": s["by_role"],
            "by_project_type": s["by_project_type"],
            "recent_trend": [r["score"] for r in s.get("recent_scores", [])]
        }

    return {"models": summary}


@app.get("/model-stats/{model_name}")
def get_single_model_stats(model_name: str):
    """View detailed stats for a specific model."""

    stats = load_model_stats()

    # url-decode the model name (e.g. qwen2.5-coder%3A32b -> qwen2.5-coder:32b)
    import urllib.parse
    model_name = urllib.parse.unquote(model_name)

    if model_name not in stats:
        raise HTTPException(status_code=404, detail=f"No stats for model '{model_name}'")

    s = stats[model_name]
    total = s["total_runs"]

    return {
        "model": model_name,
        "total_runs": total,
        "avg_score": round(s["total_score"] / total, 2) if total > 0 else 0,
        "win_rate": round((s["wins"] / total * 100), 1) if total > 0 else 0,
        "fail_rate": round((s["failures"] / total * 100), 1) if total > 0 else 0,
        "by_language": s["by_language"],
        "by_role": s["by_role"],
        "by_project_type": s["by_project_type"],
        "recent_scores": s.get("recent_scores", [])
    }


# ------------------------------------------------
# API: BRIEFING (full status overview)
# ------------------------------------------------

@app.get("/briefing")
def get_briefing():
    """
    Full status briefing combining all memory layers.
    Use this after returning from a development gap.
    """
    return build_briefing()


# ------------------------------------------------
# API: IDENTITY (Layer 1)
# ------------------------------------------------

@app.get("/identity")
def get_identity():
    """View the current identity.md content."""
    return {"content": load_identity()}


@app.put("/identity")
def update_identity(body: dict):
    """
    Replace identity.md content.
    Expects: {"content": "...new markdown content..."}
    """
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    try:
        IDENTITY_FILE.write_text(content)
        return {"status": "updated", "length": len(content)}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write identity.md: {e}")


# ------------------------------------------------
# API: PRIMER (Layer 2)
# ------------------------------------------------

@app.get("/primer")
def get_primer():
    """View the current primer.md (session state)."""
    return {"content": load_primer()}


# ------------------------------------------------
# API: GOALS
# ------------------------------------------------

@app.get("/goals")
def get_goals():
    """View the current goals.md content."""
    return {"content": load_goals()}


@app.put("/goals")
def replace_goals(body: dict):
    """
    Replace goals.md content entirely.
    Expects: {"content": "...new markdown content..."}
    """
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    try:
        GOALS_FILE.write_text(content)
        return {"status": "updated", "length": len(content)}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write goals.md: {e}")


class GoalUpdateRequest(BaseModel):
    goal_title: str
    new_status: str | None = None
    new_phase: str | None = None
    add_decision: str | None = None


@app.post("/goals/update")
def patch_goal(req: GoalUpdateRequest):
    """Update a specific goal's status, phase, or add a decision."""
    ok = update_goal_status(
        goal_title=req.goal_title,
        new_status=req.new_status,
        new_phase=req.new_phase,
        add_decision=req.add_decision
    )
    if ok:
        return {"status": "updated", "goal": req.goal_title}
    raise HTTPException(status_code=404, detail=f"Goal '{req.goal_title}' not found")


# ------------------------------------------------
# API: SESSIONS
# ------------------------------------------------

@app.get("/sessions")
def get_sessions():
    """View recent sessions (grouped runs)."""
    sessions = load_session_log()
    # return last 20 sessions, newest first
    recent = list(reversed(sessions[-20:]))
    return {
        "total": len(sessions),
        "sessions": recent
    }


@app.get("/sessions/{session_id}")
def get_session_detail(session_id: str):
    """View details of a specific session."""
    sessions = load_session_log()
    for sess in sessions:
        if sess.get("session_id") == session_id:
            return sess
    raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")


# ------------------------------------------------
# API: LIVE CONTEXT (Layer 3)
# ------------------------------------------------

@app.get("/live-context")
def get_live_context(target: str = "pi-1"):
    """
    Gather and return live system context.
    Includes loaded models, health, active runs, recent results.
    """
    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ctx = gather_live_context(target, "api-live-ctx")
    return ctx


@app.get("/live-context/formatted")
def get_live_context_formatted(target: str = "pi-1"):
    """
    Same as /live-context but returns the planner-formatted string.
    Useful for debugging what the planner actually sees.
    """
    try:
        validate_target(target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ctx = gather_live_context(target, "api-live-ctx")
    formatted = format_live_context_for_planner(ctx)
    return {"formatted": formatted}


# ------------------------------------------------
# API: OLLAMA MODELS
# ------------------------------------------------

@app.get("/models")
def get_models():
    """List all available and currently loaded models across both Ollama servers."""
    return {
        "loaded": get_loaded_models(),
        "available": get_available_models()
    }


@app.get("/models/loaded")
def get_models_loaded():
    """Quick check: which models are currently hot in memory."""
    return {"loaded": get_loaded_models()}


# ------------------------------------------------
# API: SYSTEM HEALTH
# ------------------------------------------------

@app.get("/health")
def get_health():
    """Orchestrator system health check."""

    health = get_orchestrator_health()
    active = get_active_runs()

    # check Ollama server connectivity
    ollama_status = {}
    for name, url in [("main", OLLAMA_MAIN_URL), ("judge", OLLAMA_JUDGE_URL)]:
        try:
            r = requests.get(f"{url}/api/tags", timeout=5)
            ollama_status[name] = {
                "url": url,
                "status": "online" if r.status_code == 200 else f"error ({r.status_code})",
                "model_count": len(r.json().get("models", [])) if r.status_code == 200 else 0
            }
        except requests.exceptions.RequestException as e:
            ollama_status[name] = {
                "url": url,
                "status": f"offline ({type(e).__name__})",
                "model_count": 0
            }

    # check Hindsight connectivity
    hindsight_health = {"enabled": HINDSIGHT_ENABLED}
    if HINDSIGHT_ENABLED:
        try:
            r = requests.get(f"{HINDSIGHT_URL}/v1/default/banks", timeout=5)
            hindsight_health["status"] = "online" if r.status_code == 200 else f"error ({r.status_code})"
            hindsight_health["url"] = HINDSIGHT_URL
        except requests.exceptions.RequestException as e:
            hindsight_health["status"] = f"offline ({type(e).__name__})"
            hindsight_health["url"] = HINDSIGHT_URL

    return {
        "orchestrator": health,
        "ollama_servers": ollama_status,
        "hindsight": hindsight_health,
        "active_runs": len(active),
        "uptime_indicator": "ok"
    }


# ------------------------------------------------
# API: HINDSIGHT (Layer 4)
# ------------------------------------------------

@app.get("/hindsight/status")
def hindsight_status():
    """Check if Hindsight is reachable and get bank info."""

    if not HINDSIGHT_ENABLED:
        return {"enabled": False, "status": "disabled in config"}

    try:
        r = requests.get(f"{HINDSIGHT_URL}/v1/default/banks", timeout=10)
        r.raise_for_status()
        banks = r.json()

        return {
            "enabled": True,
            "url": HINDSIGHT_URL,
            "bank_id": HINDSIGHT_BANK,
            "status": "online",
            "banks": banks
        }
    except requests.exceptions.RequestException as e:
        return {
            "enabled": True,
            "url": HINDSIGHT_URL,
            "bank_id": HINDSIGHT_BANK,
            "status": f"offline ({type(e).__name__})"
        }


class HindsightRecallRequest(BaseModel):
    query: str
    max_tokens: int = 2000


@app.post("/hindsight/recall")
def api_hindsight_recall(req: HindsightRecallRequest):
    """Recall memories from Hindsight for a given query."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_recall(req.query, "api-recall", max_tokens=req.max_tokens)

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight recall failed")

    return result


class HindsightRetainRequest(BaseModel):
    content: str


@app.post("/hindsight/retain")
def api_hindsight_retain(req: HindsightRetainRequest):
    """Manually store a memory in Hindsight."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_retain(req.content, "api-retain")

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight retain failed")

    return result


class HindsightReflectRequest(BaseModel):
    query: str


@app.post("/hindsight/reflect")
def api_hindsight_reflect(req: HindsightReflectRequest):
    """
    Ask Hindsight to reflect on accumulated memories.
    This synthesizes observations and opinions from past experiences.
    Can take 1-5 minutes depending on memory volume and model speed.
    """

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_reflect(req.query, "api-reflect")

    if result is None:
        raise HTTPException(status_code=502, detail="Hindsight reflect failed")

    return result


# ── Mental Models API ─────────────────────────────

@app.get("/hindsight/mental-models")
def api_mental_models():
    """List all Hindsight mental models with their content."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    models = hindsight_get_mental_models("api")
    return {"models": models}


@app.post("/hindsight/mental-models/{model_id}/refresh")
def api_refresh_mental_model(model_id: str):
    """Trigger a refresh of a specific mental model."""

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    result = hindsight_request(
        "POST",
        f"/v1/default/banks/{HINDSIGHT_BANK}/mental-models/{model_id}/refresh",
        timeout=30
    )

    if result is None:
        raise HTTPException(status_code=502, detail="Mental model refresh failed")

    return result


# ── PDF / File Ingestion API ─────────────────────

REFERENCE_DIR = Path("/opt/ai-orchestrator/references")
REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

# text-native extensions — read as-is, no conversion needed
TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml",
                   ".py", ".js", ".sh", ".bash", ".html", ".css", ".xml",
                   ".ini", ".cfg", ".conf", ".log", ".env", ".sql"}


def _detect_vision_model():
    """Check if any vision-capable model is available on Ollama."""
    try:
        if time.time() - _url_cache_ts > _URL_CACHE_TTL:
            _refresh_url_cache()
        vision_keywords = ["llava", "minicpm-v", "bakllava", "moondream", "vision"]
        for model_name, base_url in _url_cache.items():
            if any(kw in model_name.lower() for kw in vision_keywords):
                return model_name, base_url
    except Exception:
        pass
    return None, None


def _describe_image_with_vision(image_bytes, model, base_url, context=""):
    """Use a vision model to describe an image. Returns description string."""
    import base64
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = "Describe this image concisely. Focus on data, diagrams, charts, or technical content. "
    if context:
        prompt += f"Context: this image is from a document about {context}. "
    prompt += "Describe what information this image conveys."

    try:
        r = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
            },
            timeout=60
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        return f"[image description unavailable: {e}]"


def convert_pdf_to_markdown(pdf_path, run_id="convert"):
    """
    Convert a PDF to LLM-optimized markdown using pymupdf4llm.
    Also extracts images and optionally describes them with a vision model.
    Returns (markdown_text, image_count, images_described).
    """

    try:
        import pymupdf4llm
        import pymupdf
    except ImportError:
        # fallback: basic text extraction
        return _convert_pdf_basic(pdf_path, run_id)

    log(run_id, f"converting PDF: {pdf_path}")

    # pymupdf4llm does the heavy lifting: tables, headings, lists → markdown
    try:
        md_text = pymupdf4llm.to_markdown(str(pdf_path))
    except Exception as e:
        log(run_id, f"pymupdf4llm failed, falling back to basic extraction: {e}")
        return _convert_pdf_basic(pdf_path, run_id)

    # extract images
    image_count = 0
    images_described = 0
    vision_model, vision_url = _detect_vision_model()

    doc = None
    try:
        doc = pymupdf.open(str(pdf_path))
        pdf_stem = Path(pdf_path).stem
        img_dir = REFERENCE_DIR / f"{pdf_stem}_images"

        for page_num in range(len(doc)):
            page = doc[page_num]
            images = page.get_images(full=True)

            for img_idx, img_info in enumerate(images):
                xref = img_info[0]
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if pix.n > 4:  # CMYK → RGB
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

                    image_count += 1
                    img_filename = f"page{page_num+1}_img{img_idx+1}.png"

                    # save image file
                    img_dir.mkdir(parents=True, exist_ok=True)
                    img_path = img_dir / img_filename
                    pix.save(str(img_path))

                    # describe with vision model if available
                    description = ""
                    if vision_model and vision_url:
                        img_bytes = pix.tobytes("png")
                        description = _describe_image_with_vision(
                            img_bytes, vision_model, vision_url,
                            context=Path(pdf_path).stem
                        )
                        images_described += 1

                    # inject image reference into markdown
                    img_marker = f"\n\n**[Image: page {page_num+1}, figure {img_idx+1}]**"
                    if description:
                        img_marker += f"\n> {description}"
                    else:
                        img_marker += f"\n> *(visual content — no vision model available for description)*"
                    img_marker += f"\n> *Saved: {img_path}*\n"

                    # append to end of markdown (images are supplementary)
                    md_text += img_marker

                    pix = None  # free memory
                except Exception as e:
                    log(run_id, f"image extraction failed (page {page_num+1}, img {img_idx+1}): {e}")
    except Exception as e:
        log(run_id, f"image extraction phase failed (non-fatal): {e}")
    finally:
        if doc:
            doc.close()

    log(run_id, f"PDF converted: {len(md_text)} chars, {image_count} images, {images_described} described")
    return md_text, image_count, images_described


def _convert_pdf_basic(pdf_path, run_id="convert"):
    """Fallback: basic page-by-page text extraction."""
    doc = None
    try:
        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        pages = []
        for i, page in enumerate(doc):
            text = page.get_text("text").strip()
            if text:
                pages.append(f"## Page {i+1}\n\n{text}")
        md = "\n\n---\n\n".join(pages) if pages else "[empty PDF]"
        return md, 0, 0
    except Exception as e:
        log(run_id, f"basic PDF extraction failed: {e}")
        return f"[PDF extraction failed: {e}]", 0, 0
    finally:
        if doc:
            doc.close()


def convert_file_to_markdown(file_path, run_id="convert"):
    """
    Convert any supported file to markdown for LLM consumption.
    PDFs get full conversion. Text files get wrapped in code fences.
    Returns (markdown_text, metadata_dict).
    """

    path = Path(file_path)
    ext = path.suffix.lower()
    name = path.name

    if ext == ".pdf":
        md_text, img_count, img_described = convert_pdf_to_markdown(file_path, run_id)
        return md_text, {"type": "pdf", "images": img_count, "images_described": img_described}

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(errors="replace")
            # wrap code files in fences for clarity
            if ext in {".py", ".js", ".sh", ".bash", ".html", ".css", ".sql", ".xml"}:
                lang = ext.lstrip(".")
                md_text = f"# {name}\n\n```{lang}\n{content}\n```"
            elif ext in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}:
                md_text = f"# {name}\n\n```{ext.lstrip('.')}\n{content}\n```"
            elif ext == ".csv":
                # convert CSV to markdown table (first 50 rows)
                lines = content.strip().splitlines()[:51]
                if lines:
                    header = lines[0]
                    md_text = f"# {name}\n\n| {header.replace(',', ' | ')} |\n"
                    md_text += "| " + " | ".join(["---"] * len(header.split(","))) + " |\n"
                    for line in lines[1:50]:
                        md_text += f"| {line.replace(',', ' | ')} |\n"
                    if len(lines) > 50:
                        md_text += f"\n*({len(content.splitlines())} total rows, showing first 50)*\n"
                else:
                    md_text = f"# {name}\n\n[empty CSV]"
            else:
                md_text = f"# {name}\n\n{content}"

            return md_text, {"type": ext.lstrip(".")}
        except Exception as e:
            return f"[file read failed: {e}]", {"type": ext.lstrip("."), "error": str(e)}

    # unsupported extension — try reading as text
    try:
        content = path.read_text(errors="replace")
        return f"# {name}\n\n{content}", {"type": "text"}
    except Exception:
        return f"[unsupported file type: {ext}]", {"type": "unsupported"}


def load_reference_content(filename):
    """Load the markdown version of a reference file. Returns content string."""

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)

    # check for pre-converted markdown version first
    md_path = REFERENCE_DIR / f"{Path(safe_name).stem}.md"
    if md_path.exists() and safe_name != md_path.name:
        return md_path.read_text(errors="replace")

    # load original
    orig_path = REFERENCE_DIR / safe_name
    if not orig_path.exists():
        return ""

    # if it's already markdown/text, read directly
    ext = orig_path.suffix.lower()
    if ext in TEXT_EXTENSIONS or ext == ".md":
        return orig_path.read_text(errors="replace")

    # for PDFs and other files, attempt on-the-fly conversion
    if ext == ".pdf" or ext in TEXT_EXTENSIONS:
        try:
            md_text, _ = convert_file_to_markdown(str(orig_path), "load-ref")
            # cache the conversion for next time
            if md_text and not md_text.startswith("["):
                md_path.write_text(md_text)
            return md_text
        except Exception:
            return ""

    return ""


MAX_REFERENCE_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB per file
MAX_REFERENCE_CONTENT_CHARS = 120_000            # ~30k tokens, safe for most models


@app.post("/references/upload")
async def upload_reference(file: UploadFile = File(...)):
    """
    Upload a file as a reference document.
    PDFs are auto-converted to markdown. Text files are stored as-is.
    Everything gets ingested into Hindsight for RAG.
    """

    filename = file.filename or f"ref_{uuid.uuid4().hex[:8]}"
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    local_path = REFERENCE_DIR / safe_name

    # save original file with size limit
    content = await file.read()
    if len(content) > MAX_REFERENCE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large ({len(content)//1024//1024}MB). Max {MAX_REFERENCE_UPLOAD_BYTES//1024//1024}MB.")
    with open(local_path, "wb") as f:
        f.write(content)

    # convert to markdown
    md_text, meta = convert_file_to_markdown(str(local_path), "api-upload")

    # save markdown version alongside original (for PDF → .md)
    md_filename = f"{Path(safe_name).stem}.md"
    md_path = REFERENCE_DIR / md_filename
    if local_path.suffix.lower() != ".md":
        md_path.write_text(md_text)

    # ingest markdown into Hindsight
    hindsight_result = None
    if HINDSIGHT_ENABLED:
        hindsight_result = hindsight_retain(
            f"Reference document '{filename}' uploaded.\n\n{md_text[:3000]}",
            "api-upload"
        )

    return {
        "filename": safe_name,
        "markdown_filename": md_filename,
        "path": str(local_path),
        "size": len(content),
        "markdown_size": len(md_text),
        "conversion": meta,
        "hindsight_ingested": hindsight_result is not None,
    }


@app.get("/references")
def list_references():
    """List all uploaded reference documents with their markdown status."""

    refs = []
    seen_stems = set()

    for f in sorted(REFERENCE_DIR.iterdir()):
        try:
            if not f.is_file() or f.name.endswith("_images"):
                continue
            stem = f.stem
            # skip markdown duplicates of PDFs (show the original instead)
            if f.suffix.lower() == ".md" and stem in seen_stems:
                continue

            md_path = REFERENCE_DIR / f"{stem}.md"
            has_markdown = md_path.exists() and f.suffix.lower() != ".md"
            st = f.stat()

            refs.append({
                "filename": f.name,
                "path": str(f),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "type": f.suffix.lstrip(".").lower() or "unknown",
                "has_markdown": has_markdown,
                "markdown_filename": f"{stem}.md" if has_markdown else None,
            })
            seen_stems.add(stem)
        except (FileNotFoundError, OSError):
            continue

    return {"references": refs}


@app.get("/references/{filename}/content")
def get_reference_content(filename: str):
    """Get the markdown content of a reference (converted if PDF)."""

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    stem = Path(safe_name).stem

    # try markdown version first
    md_path = REFERENCE_DIR / f"{stem}.md"
    if md_path.exists():
        return {"filename": safe_name, "content": md_path.read_text(errors="replace"), "format": "markdown"}

    # try original
    orig_path = REFERENCE_DIR / safe_name
    if not orig_path.exists():
        raise HTTPException(status_code=404, detail="Reference not found")

    try:
        content = orig_path.read_text(errors="replace")
        return {"filename": safe_name, "content": content, "format": "text"}
    except Exception:
        raise HTTPException(status_code=415, detail="Cannot read file as text")


@app.delete("/references/{filename}")
def delete_reference(filename: str):
    """Delete a reference document and its converted markdown."""

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    path = REFERENCE_DIR / safe_name
    stem = Path(safe_name).stem

    if not path.exists():
        raise HTTPException(status_code=404, detail="Reference not found")

    path.unlink(missing_ok=True)

    # also remove markdown version if it exists
    md_path = REFERENCE_DIR / f"{stem}.md"
    if md_path != path:
        md_path.unlink(missing_ok=True)

    # remove extracted images dir if it exists
    img_dir = REFERENCE_DIR / f"{stem}_images"
    if img_dir.exists() and img_dir.is_dir():
        import shutil
        shutil.rmtree(img_dir, ignore_errors=True)

    return {"deleted": safe_name}


# ── Graph Data API (for enriched UI nodes) ────────

@app.get("/graph-data")
def get_graph_data():
    """
    Combined endpoint for the UI node cluster.
    Returns nodes (projects, models, targets, errors) with enriched metadata
    from vault notes, memory, and mental models.
    """

    # load all memory sources
    index = load_prompt_index()
    neg_memory = json.loads(NEGATIVE_MEMORY.read_text()) if NEGATIVE_MEMORY.exists() else []
    model_stats = load_model_stats()

    # build project summaries
    projects = {}
    for entry in index:
        name = entry.get("project", "")
        if not name:
            continue
        if name not in projects:
            projects[name] = {
                "type": "project", "name": name,
                "runs": 0, "successes": 0, "best_score": 0,
                "languages": set(), "models": set(), "targets": set(),
                "last_prompt": "", "last_run": "",
            }
        p = projects[name]
        p["runs"] += 1
        if entry.get("success"):
            p["successes"] += 1
        p["best_score"] = max(p["best_score"], entry.get("score", 0))
        if entry.get("language"):
            p["languages"].add(entry["language"])
        if entry.get("winning_model"):
            p["models"].add(entry["winning_model"])
        p["last_prompt"] = entry.get("prompt", p["last_prompt"])
        p["last_run"] = entry.get("run_id", p["last_run"])

    # serialize sets
    for p in projects.values():
        p["languages"] = list(p["languages"])
        p["models"] = list(p["models"])
        p["targets"] = list(p["targets"])
        p["success_rate"] = round(p["successes"] / max(p["runs"], 1) * 100)

    # build error summaries from negative memory
    errors = []
    for entry in neg_memory[-20:]:
        errors.append({
            "type": "error",
            "project": entry.get("project", ""),
            "stage": entry.get("failure_stage", "unknown"),
            "summary": (entry.get("error_summary", ""))[:100],
            "models": entry.get("models_tried", []),
            "language": entry.get("language", ""),
        })

    # build model summaries
    models = {}
    for name, stats in model_stats.items():
        models[name] = {
            "type": "model", "name": name,
            **stats,
        }

    # build target summaries
    targets = {}
    for name in SSH_TARGETS:
        targets[name] = {
            "type": "target", "name": name,
            "host": SSH_TARGETS[name].get("host", ""),
        }
        # load target identity if available
        identity = load_target_identity(name)
        if identity:
            targets[name]["identity"] = identity[:500]

    # mental models (if available)
    mental_models = []
    if HINDSIGHT_ENABLED:
        mental_models = hindsight_get_mental_models("graph-data")

    return {
        "projects": list(projects.values()),
        "models": list(models.values()),
        "targets": list(targets.values()),
        "errors": errors,
        "mental_models": [
            {"id": m["id"], "name": m["name"], "content": m.get("content", "")[:1000],
             "tags": m.get("tags", []), "last_refreshed": m.get("last_refreshed_at", "")}
            for m in mental_models
        ],
    }


# ------------------------------------------------
# API: TARGET IDENTITY (per-node)
# ------------------------------------------------

@app.get("/identity/targets")
def list_target_identities():
    """List all target identity files."""
    identities = {}
    for target_name in SSH_TARGETS:
        content = load_target_identity(target_name)
        identities[target_name] = {
            "content": content,
            "length": len(content)
        }
    return {"targets": identities}


@app.get("/identity/target/{target_name}")
def get_target_identity(target_name: str):
    """View the identity.md for a specific target node."""
    if target_name not in SSH_TARGETS:
        raise HTTPException(status_code=404, detail=f"Unknown target: {target_name}")
    content = load_target_identity(target_name)
    return {"target": target_name, "content": content}


@app.put("/identity/target/{target_name}")
def update_target_identity(target_name: str, body: dict):
    """
    Replace the identity.md for a specific target node.
    Expects: {"content": "...new markdown content..."}
    """
    if target_name not in SSH_TARGETS:
        raise HTTPException(status_code=404, detail=f"Unknown target: {target_name}")

    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    ok = save_target_identity(target_name, content)
    if ok:
        return {"status": "updated", "target": target_name, "length": len(content)}
    raise HTTPException(status_code=500, detail=f"Could not write identity for {target_name}")


@app.post("/identity/target/{target_name}/profile")
def profile_target(target_name: str):
    """
    Force a re-profile of a target's hardware by running environment inspection.
    Updates the Hardware section in the target identity file.
    """
    try:
        validate_target(target_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = f"profile-{target_name}"
    env = environment_inspector(target_name, run_id)
    auto_update_target_identity(target_name, env, run_id)

    return {
        "status": "profiled",
        "target": target_name,
        "environment": env,
        "identity": load_target_identity(target_name)
    }


# ------------------------------------------------
# API: NOTIFICATIONS
# ------------------------------------------------

@app.get("/notifications/config")
def get_notification_config():
    """View current notification configuration (redacts Gotify token)."""
    return {
        "enabled": NOTIFY_ENABLED,
        "service": NOTIFY_SERVICE,
        "ntfy_url": NTFY_URL,
        "ntfy_topic": NTFY_TOPIC,
        "ntfy_priority": NTFY_PRIORITY,
        "gotify_url": GOTIFY_URL,
        "gotify_token": "***" if GOTIFY_TOKEN else "",
        "gotify_priority": GOTIFY_PRIORITY,
        "on_success": NOTIFY_ON_SUCCESS,
        "on_failure": NOTIFY_ON_FAILURE,
    }


class TestNotificationRequest(BaseModel):
    title: str | None = "Test Notification"
    message: str | None = "This is a test from the AI Orchestrator."


@app.post("/notifications/test")
def test_notification(req: TestNotificationRequest = None):
    """Send a test notification to verify the configuration."""

    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications are disabled in config")

    title = "Test Notification"
    message = "This is a test from the AI Orchestrator."

    if req:
        title = req.title or title
        message = req.message or message

    send_notification(
        title=title,
        message=message,
        priority="default",
        tags=["bell", "test_tube"]
    )

    return {"status": "sent", "service": NOTIFY_SERVICE}


@app.post("/notifications/quick-actions")
def api_quick_actions():
    """Send a remote-control notification with quick action buttons."""
    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications disabled")
    send_quick_actions_notification()
    return {"status": "sent", "service": NOTIFY_SERVICE}


@app.post("/notifications/cheatsheet")
def api_send_cheatsheet(run_id: str = None, project_name: str = None, target: str = None):
    """Send an API cheatsheet notification with curl commands."""
    if not NOTIFY_ENABLED:
        raise HTTPException(status_code=503, detail="Notifications disabled")
    send_api_cheatsheet_notification(run_id=run_id, project_name=project_name, target=target)
    return {"status": "sent"}




# ------------------------------------------------
# API: VAULT (L5)
# ------------------------------------------------

@app.get("/vault/status")
def vault_status():
    """Check vault configuration and note counts."""
    counts = {}
    for subdir in ["runs", "projects", "models", "targets", "errors", "daily"]:
        dir_path = Path(VAULT_LOCAL_DIR) / subdir
        if dir_path.exists():
            counts[subdir] = len(list(dir_path.glob("*.md")))
        else:
            counts[subdir] = 0

    return {
        "enabled": VAULT_ENABLED,
        "local_dir": VAULT_LOCAL_DIR,
        "remote_host": VAULT_REMOTE_HOST or None,
        "remote_dir": VAULT_REMOTE_DIR if VAULT_REMOTE_HOST else None,
        "sync_enabled": VAULT_SYNC_ENABLED,
        "nas_enabled": VAULT_NAS_ENABLED,
        "nas_path": VAULT_NAS_PATH if VAULT_NAS_ENABLED else None,
        "note_counts": counts,
        "total_notes": sum(counts.values())
    }


@app.post("/vault/sync")
def vault_force_sync():
    """Force a full vault sync to the remote NoteDiscovery host."""
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")
    if not VAULT_REMOTE_HOST:
        raise HTTPException(status_code=400, detail="No remote host configured")

    ok_remote = vault_sync_to_remote("api-vault-sync")
    ok_nas = False
    if VAULT_NAS_ENABLED:
        ok_nas = vault_sync_to_nas("api-vault-sync")
    return {
        "status": "synced",
        "notediscovery": "synced" if ok_remote else "failed",
        "nas": "synced" if ok_nas else ("failed" if VAULT_NAS_ENABLED else "disabled")
    }


@app.post("/vault/sync-nas")
def vault_force_sync_nas():
    """Force sync vault to NAS mount point."""
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")
    if not VAULT_NAS_ENABLED:
        raise HTTPException(status_code=503, detail="NAS sync is disabled")

    ok = vault_sync_to_nas("api-vault-nas")
    return {"status": "synced" if ok else "failed", "path": VAULT_NAS_PATH}


@app.post("/vault/rebuild")
def vault_rebuild():
    """
    Rebuild all vault notes from current memory state.
    Useful after importing data or fixing issues.
    Regenerates project notes, model notes, target notes, daily digest, and index.
    """
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")

    run_id = "vault-rebuild"
    rebuilt = []

    # rebuild project notes
    index = load_prompt_index()
    projects = list(set(e.get("project", "") for e in index if e.get("project")))
    for project in projects:
        vault_write_project_note(project, run_id)
        rebuilt.append(f"projects/{_vault_safe_name(project)}")

    # rebuild model notes
    stats = load_model_stats()
    for model_name in stats:
        vault_write_model_note(model_name, run_id)
        rebuilt.append(f"models/{_vault_safe_name(model_name)}")

    # rebuild target notes
    for target_name in SSH_TARGETS:
        vault_write_target_note(target_name, run_id)
        rebuilt.append(f"targets/{_vault_safe_name(target_name)}")

    # daily digest
    vault_write_daily_digest(run_id)
    rebuilt.append("daily digest")

    # index
    vault_write_index(run_id)
    rebuilt.append("index")

    # sync all
    if VAULT_SYNC_ENABLED and VAULT_REMOTE_HOST:
        vault_sync_to_remote(run_id)

    return {"status": "rebuilt", "notes": rebuilt}


@app.get("/vault/note/{subdir}/{filename}")
def vault_get_note(subdir: str, filename: str):
    """Read a specific vault note."""
    if not VAULT_ENABLED:
        raise HTTPException(status_code=503, detail="Vault is disabled")

    if subdir not in ("runs", "projects", "models", "targets", "errors", "daily"):
        raise HTTPException(status_code=400, detail="Invalid vault subdirectory")

    if not SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # prevent path traversal via symlinks or ..
    filepath = (Path(VAULT_LOCAL_DIR) / subdir / filename).resolve()
    vault_base = Path(VAULT_LOCAL_DIR).resolve()
    if not str(filepath).startswith(str(vault_base)):
        raise HTTPException(status_code=400, detail="Invalid path")

    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Note not found")

    try:
        content = filepath.read_text()
        return {"subdir": subdir, "filename": filename, "content": content}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not read note: {e}")


@app.post("/hindsight/reflect/auto")
def api_hindsight_auto_reflect():
    """
    Trigger automatic reflection on key orchestrator topics.
    Asks Hindsight to synthesize insights about model performance,
    common failure patterns, and language/task-type trends.
    """

    if not HINDSIGHT_ENABLED:
        raise HTTPException(status_code=503, detail="Hindsight is disabled")

    queries = [
        "Which models perform best for which programming languages and task types?",
        "What are the most common failure patterns and how can they be avoided?",
        "What deployment and execution patterns have been most reliable?",
    ]

    results = []
    for q in queries:
        result = hindsight_reflect(q, "auto-reflect")
        results.append({
            "query": q,
            "result": result
        })

    return {"reflections": results}


# ------------------------------------------------
# API: DASHBOARD ENDPOINTS
# ------------------------------------------------

@app.get("/targets")
def list_targets():
    """List all configured SSH targets."""
    targets = []
    for name, cfg in SSH_TARGETS.items():
        targets.append({
            "name": name,
            "host": cfg["host"],
            "username": cfg["username"]
        })
    return {"targets": targets}


@app.get("/tools")
def list_tools():
    return {"tools": load_tool_registry()}


@app.post("/tools")
def create_tool(tool: dict):
    registry = load_tool_registry()
    if any(t["name"] == tool.get("name") for t in registry):
        raise HTTPException(status_code=400, detail=f"Tool '{tool.get('name')}' already exists")
    registry.append(tool)
    _save_tool_registry(registry)
    return tool


@app.put("/tools/{name}")
def update_tool(name: str, tool: dict):
    registry = load_tool_registry()
    idx = next((i for i, t in enumerate(registry) if t["name"] == name), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    registry[idx] = tool
    _save_tool_registry(registry)
    return tool


@app.delete("/tools/{name}")
def delete_tool(name: str):
    registry = load_tool_registry()
    new_registry = [t for t in registry if t["name"] != name]
    if len(new_registry) == len(registry):
        raise HTTPException(status_code=404, detail=f"Tool '{name}' not found")
    _save_tool_registry(new_registry)
    return {"deleted": name}


# ------------------------------------------------
# AGENT CONFIG MANAGEMENT
# ------------------------------------------------

@app.get("/agents")
def api_list_agents():
    """List all available agent roles and their configs."""
    agents = load_all_agents()
    return {"agents": {role: cfg.to_dict() for role, cfg in agents.items()}}


@app.get("/agents/{role}")
def api_get_agent(role: str):
    """Get full config for a specific agent role."""
    try:
        agent = load_agent(role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent role '{role}' not found")
    result = agent.to_dict()
    result["system_prompt"] = agent.system_prompt_template
    result["user_prompt"] = agent.user_prompt_template
    result["user_prompt_multi"] = agent.user_prompt_multi_template
    result["schema"] = agent.schema
    result["variants"] = agent.variants
    return result


@app.put("/agents/{role}/prompt/{prompt_type}")
def api_update_agent_prompt(role: str, prompt_type: str, body: dict):
    """Update a prompt file for an agent role. prompt_type: system_prompt, user_prompt, user_prompt_multi."""
    valid_types = {"system_prompt", "user_prompt", "user_prompt_multi"}
    if prompt_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"prompt_type must be one of: {valid_types}")

    try:
        agent = load_agent(role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent role '{role}' not found")

    content = body.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="Missing 'content' field")

    prompt_path = os.path.join(agent.dir, f"{prompt_type}.md")
    with open(prompt_path, "w") as f:
        f.write(content)

    # reload this agent
    load_agent(role, force_reload=True)
    return {"updated": prompt_type, "role": role}


@app.post("/agents/reload")
def api_reload_agents():
    """Force-reload all agent configs from disk."""
    agents = reload_agents()
    return {"reloaded": list(agents.keys())}


@app.get("/agents/{role}/variants")
def api_get_variants(role: str):
    """Get language variants for an agent role."""
    try:
        agent = load_agent(role)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent role '{role}' not found")
    return {"role": role, "variants": agent.variants}


# ------------------------------------------------
# DREAM API
# ------------------------------------------------

@app.post("/dream")
def api_run_dream():
    """Trigger a manual dream cycle (memory consolidation)."""
    available = set(_url_cache.keys()) if _url_cache else None
    report = run_dream(available_models=available, log_fn=log)
    return report


@app.get("/dream/log")
def api_dream_log():
    """Get dream cycle history."""
    return {"log": dream_load_json(DREAM_LOG, [])}


@app.get("/dream/health")
def api_dream_health():
    """Get current memory health without running a full dream cycle."""
    log_entries = dream_load_json(DREAM_LOG, [])
    if log_entries:
        latest = log_entries[-1]
        return {
            "health_score": latest.get("health_score", None),
            "health_rating": latest.get("health_rating", None),
            "last_dream": latest.get("timestamp", None),
            "elapsed": latest.get("elapsed", None),
        }
    return {"health_score": None, "health_rating": "unknown", "last_dream": None}


# ------------------------------------------------
# GATES API
# ------------------------------------------------

@app.get("/gates")
def api_list_gates():
    """List all gate rules with summary stats."""
    return get_gates_summary()


@app.post("/gates")
def api_add_gate(body: dict):
    """Add a manual gate rule."""
    pattern = body.get("pattern")
    reason = body.get("reason")
    if not pattern or not reason:
        raise HTTPException(status_code=400, detail="'pattern' and 'reason' are required")
    severity = body.get("severity", "block")
    gate = add_gate(pattern, reason, source="manual", severity=severity)
    return gate


@app.delete("/gates/{gate_id}")
def api_remove_gate(gate_id: str):
    """Remove a gate rule by ID."""
    remove_gate(gate_id)
    return {"deleted": gate_id}


@app.put("/gates/{gate_id}/toggle")
def api_toggle_gate(gate_id: str, body: dict):
    """Enable or disable a gate rule."""
    enabled = body.get("enabled", True)
    toggle_gate(gate_id, enabled)
    return {"gate_id": gate_id, "enabled": enabled}


@app.get("/gates/lessons")
def api_list_lessons():
    """Get lessons summary with recent incidents."""
    return get_lessons_summary()


@app.post("/gates/consolidate")
def api_consolidate_gates(body: dict = None):
    """Manually trigger lesson consolidation. Pass dry_run=true to preview."""
    dry_run = (body or {}).get("dry_run", False)
    report = consolidate_lessons(dry_run=dry_run, log_fn=log)
    return report


@app.get("/logs/{run_id}/tail")
def tail_log(run_id: str, lines: int = 80):
    """Return the last N lines of a run's log file."""
    log_path = Path(LOG_DIR) / f"{run_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log not found")
    try:
        content = log_path.read_text()
        all_lines = content.strip().splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {
            "run_id": run_id,
            "total_lines": len(all_lines),
            "lines": tail
        }
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/control/pause")
def toggle_pause():
    """Toggle orchestrator pause state. When paused, new runs are rejected."""
    global ORCHESTRATOR_PAUSED
    ORCHESTRATOR_PAUSED = not ORCHESTRATOR_PAUSED
    return {"paused": ORCHESTRATOR_PAUSED}


@app.get("/control/status")
def control_status():
    """Get orchestrator control status."""
    return {
        "paused": ORCHESTRATOR_PAUSED,
        "active_runs": len([r for r in RUN_STATUS.values() if not r.get("completed", True)])
    }


@app.post("/control/restart")
def restart_orchestrator():
    """
    Restart the orchestrator service.
    Returns immediately; the service restarts in the background.
    """
    import subprocess as _sp
    try:
        _sp.Popen(
            ["bash", "-c", "sleep 1 && systemctl restart ai-orchestrator"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
        )
        return {"status": "restarting", "message": "Service will restart in ~1 second"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restart failed: {e}")


# ------------------------------------------------
# WEB UI
# ------------------------------------------------

UI_DIR = Path("/opt/ai-orchestrator/ui")

@app.get("/ui", response_class=HTMLResponse)
def serve_ui():
    """Serve the 3D graph visualization."""
    graph_path = UI_DIR / "graph.html"
    if not graph_path.exists():
        raise HTTPException(status_code=404, detail="UI not found. Place graph.html in /opt/ai-orchestrator/ui/")
    return graph_path.read_text()


@app.get("/ui/{filename}")
def serve_ui_file(filename: str):
    """Serve additional UI files (CSS, JS, etc.)."""
    filepath = UI_DIR / filename
    if not filepath.exists() or ".." in filename:
        raise HTTPException(status_code=404)
    return FileResponse(filepath)


# ------------------------------------------------
# WEBSOCKET: LIVE STREAMING
# ------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Live streaming endpoint. Clients receive JSON messages:
      {"type": "log",    "run_id": "...", "line": "...", "phase": "..."}
      {"type": "status", "run_id": "...", "phase": "...", "score": N, ...}
    """
    await ws.accept()
    with _ws_lock:
        _ws_clients.append(ws)
    try:
        while True:
            # keep connection alive; client can send pings or filter commands
            data = await ws.receive_text()
            # echo back as heartbeat acknowledgement
            await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            try:
                _ws_clients.remove(ws)
            except ValueError:
                pass


# ------------------------------------------------
# API: CONFIG (read / write config.json from UI)
# ------------------------------------------------

@app.get("/config")
def get_config():
    """Return current config.json contents."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Could not read config: {e}")


class ConfigUpdate(BaseModel):
    config: dict


@app.put("/config")
def update_config(req: ConfigUpdate):
    """
    Update config.json. Takes the full config object.
    Backs up current config before writing.
    Returns the saved config. Requires service restart to take effect.
    """
    import copy

    # validate: must have required top-level keys
    required = {"ollama", "ssh_targets"}
    missing = required - set(req.config.keys())
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required keys: {missing}")

    # backup current config
    try:
        with open(CONFIG_PATH) as f:
            current = json.load(f)
    except (json.JSONDecodeError, OSError):
        current = {}

    backup_path = CONFIG_PATH + ".auto.bak"
    try:
        with open(backup_path, "w") as f:
            json.dump(current, f, indent=2)
    except OSError:
        pass

    # write new config
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(req.config, f, indent=2)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write config: {e}")

    return {"status": "saved", "config": req.config, "restart_required": True}


# ------------------------------------------------
# API: PROJECTS (list all deployed projects across targets)
# ------------------------------------------------

@app.get("/projects/deployed")
def list_deployed_projects():
    """List all deployed projects across all targets with metadata."""
    results = []
    for name, cfg in SSH_TARGETS.items():
        resolve = ssh_command(name, f"echo {DEPLOY_BASE}")
        base = resolve["stdout"].strip()
        if not base:
            continue

        # list project dirs
        ls_result = ssh_command(name, f"ls -1 {shlex.quote(base)} 2>/dev/null")
        if ls_result["returncode"] != 0:
            continue

        for project_name in ls_result["stdout"].strip().splitlines():
            project_name = project_name.strip()
            if not project_name:
                continue

            project_dir = f"{base}/{project_name}"

            # try to read project.json metadata
            meta_result = ssh_command(name, f"cat {shlex.quote(project_dir)}/project.json 2>/dev/null")
            meta = {}
            if meta_result["returncode"] == 0 and meta_result["stdout"].strip():
                try:
                    meta = json.loads(meta_result["stdout"])
                except (json.JSONDecodeError, ValueError):
                    pass

            results.append({
                "target": name,
                "project_name": project_name,
                "deploy_path": project_dir,
                "score": meta.get("score"),
                "language": meta.get("language"),
                "deployed_at": meta.get("deployed_at"),
                "run_id": meta.get("run_id"),
                "entrypoint": meta.get("entrypoint"),
                "prompt": meta.get("prompt", "")[:200],
            })

    return {"projects": results}

