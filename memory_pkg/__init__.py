"""Memory layers — positive/negative recall, stats, identity/primer/goals,
sessions, targets, Hindsight client, vault writers.

Single-file for the initial split (commit 0.g.6). The plan calls for
further splitting into embedding.py / stats.py / layers.py / sessions.py /
targets.py / hindsight.py / vault.py / context.py — defer that to a
follow-up.

Note on naming: this Python package is named `memory_pkg/` rather than
`memory/` to avoid the namespace clash with the data directory at
/opt/ai-orchestrator/memory/ (which holds the actual JSON / markdown
state files this code reads and writes).

Vault-to-memory coupling note: vault_write_model_note / vault_write_project_note /
vault_write_target_note still call load_model_stats / load_negative_memory /
load_prompt_index directly. The plan calls for breaking that with parameter
injection — deferred; the in-package call cycle works correctly.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import requests

from core.config import (
    CONFIG, OLLAMA_EMBED, OLLAMA_PLANNER_CHAT, OLLAMA_MAIN_URL,
    HINDSIGHT_URL, HINDSIGHT_BANK, HINDSIGHT_ENABLED, HINDSIGHT_TIMEOUT,
    TIMEOUT_EMBEDDING, TIMEOUT_HINDSIGHT_RETAIN, TIMEOUT_HINDSIGHT_RECALL,
    TIMEOUT_HINDSIGHT_REFLECT, TIMEOUT_VAULT_SYNC, TIMEOUT_VAULT_NAS_SYNC,
    SIMILARITY_THRESHOLD, REUSE_SCORE_THRESHOLD,
    MAX_PROMPT_INDEX_ENTRIES, MAX_EMBED_CACHE_ENTRIES,
    SSH_TARGETS, SSH_TIMEOUT,
    VAULT_ENABLED, VAULT_LOCAL_DIR, VAULT_REMOTE_HOST, VAULT_REMOTE_USER,
    VAULT_REMOTE_KEY, VAULT_REMOTE_DIR, VAULT_SYNC_ENABLED,
    VAULT_NAS_ENABLED, VAULT_NAS_PATH,
)
from core.locks import locked_read_json, locked_write_json
from core.paths import (
    PROMPT_INDEX, EMBED_CACHE, NEGATIVE_MEMORY, MODEL_STATS,
    IDENTITY_FILE, PRIMER_FILE, GOALS_FILE, SESSION_LOG,
    TARGET_IDENTITY_DIR, CAMPAIGNS_FILE,
)
from core.runtime import RUN_STATUS, log
from prefect import task


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

def load_campaigns():
    """Load campaigns map keyed by campaign_id (Phase 1.1)."""
    return locked_read_json(CAMPAIGNS_FILE, {})

def save_campaigns(data, changed_ids=None):
    """Persist the campaigns map to JSON, then mirror to Postgres (Phase 2.1).

    JSON stays canonical. ``changed_ids`` scopes the Postgres dual-write
    to the campaigns the caller actually mutated; when ``None`` (the
    default — used by reconcile-on-startup), every campaign in the map
    is upserted.

    A Postgres failure is logged + swallowed inside db_writethrough; the
    JSON write must complete first or the dual-write is skipped.
    """
    locked_write_json(CAMPAIGNS_FILE, data)
    try:
        # Lazy import — most memory_pkg consumers don't need core.db.
        from core import db_writethrough
        db_writethrough.mirror_campaigns(data, changed_ids=changed_ids)
    except Exception:
        # mirror_campaigns already swallows; this catch covers a stray
        # import-time error so save_campaigns never raises.
        pass


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


@task(name="update_memory", retries=2)
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


@task(name="update_negative_memory", retries=2)
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

    # Phase 2.1 dual-write: mirror today's row into Postgres.
    # Counters increment atomically; jsonb breakdowns are best-effort
    # snapshots of the JSON aggregate (reconcile re-syncs on startup).
    try:
        from core import db_writethrough
        db_writethrough.mirror_model_stats_daily(
            model=model,
            score=score,
            was_winner=was_winner,
            succeeded=succeeded,
            by_language=s.get("by_language"),
            by_role=s.get("by_role"),
            by_project_type=s.get("by_project_type"),
        )
    except Exception:
        # mirror_model_stats_daily already swallows; this is belt-and-braces.
        pass


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


@task(name="rewrite_primer", retries=2)
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


@task(name="record_session", retries=2)
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

# TARGET_IDENTITY_DIR imported from core.paths above (which also mkdir's it)


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
            print("Hindsight bank creation returned no response (may already exist)")

    except (requests.exceptions.RequestException, OSError, ValueError) as e:
        print(f"Hindsight bank setup failed (non-fatal): {e}")


# attempt bank creation on startup (non-blocking, best-effort)
if HINDSIGHT_ENABLED:
    try:
        hindsight_ensure_bank()
    except (requests.exceptions.RequestException, OSError, ValueError):
        print("Hindsight not available at startup (will retry on first use)")


@task(name="hindsight_retain", retries=2)
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
        content += "Code executed successfully (exit code 0). "
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

    campaign_id = (RUN_STATUS.get(run_id) or {}).get("campaign_id")
    campaign_line = f"campaign_id: {campaign_id}\n" if campaign_id else ""

    content = f"""---
run_id: {run_id}
{campaign_line}project: {project_name}
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


def vault_write_campaign_note(campaign):
    """Write or update the campaign aggregator note (Phase 1.1).

    Idempotent — overwrites the same file on each call so progress can be
    re-emitted after every child run. Filename is stable across the
    campaign's lifetime (date + safe_name + short_id).
    """
    if not VAULT_ENABLED:
        return None

    campaign_id = campaign.get("id", "")
    if not campaign_id:
        return None

    name = campaign.get("name", "campaign")
    safe_name = _vault_safe_name(name)
    short_id = campaign_id[:8]
    created = campaign.get("created_at", "")
    date_str = (created.split("T")[0] if "T" in created
                else datetime.utcnow().strftime("%Y-%m-%d"))
    filename = f"{date_str}_{safe_name}_{short_id}.md"

    runs = campaign.get("runs", [])
    scores = [r.get("score", 0) for r in runs if r.get("score") is not None]
    mean_score = sum(scores) / len(scores) if scores else 0
    status = campaign.get("status", "queued")

    front_matter = (
        "---\n"
        f"campaign_id: {campaign_id}\n"
        f"name: {name}\n"
        f"status: {status}\n"
        f"run_count: {len(runs)}\n"
        f"mean_score: {mean_score:.2f}\n"
        f"created_at: {campaign.get('created_at', '')}\n"
        f"updated_at: {campaign.get('updated_at', '')}\n"
        f"completed_at: {campaign.get('completed_at') or ''}\n"
        "tags:\n  - campaign\n"
        f"  - {status}\n"
        "---\n\n"
    )

    overview = f"# {name} — Campaign {short_id}\n\n"
    if campaign.get("description"):
        overview += f"{campaign['description']}\n\n"
    overview += (
        "| Field | Value |\n|-------|-------|\n"
        f"| Status | **{status}** |\n"
        f"| Runs | {len(runs)} |\n"
        f"| Mean score | {mean_score:.2f} |\n"
        f"| Created | {campaign.get('created_at', '')} |\n"
        f"| Updated | {campaign.get('updated_at', '')} |\n"
    )
    if campaign.get("completed_at"):
        overview += f"| Completed | {campaign['completed_at']} |\n"
    overview += "\n"

    params_section = "## Parameters\n\n"
    grid = campaign.get("params", {}) or {}
    if grid:
        for k, v in grid.items():
            params_section += f"- **{k}**: {v}\n"
    else:
        params_section += "_(no parameter sweep — single run)_\n"
    params_section += "\n"
    if campaign.get("max_runs") is not None:
        params_section += f"_max_runs cap: {campaign['max_runs']}_\n\n"

    runs_section = "## Runs\n\n"
    if runs:
        runs_section += "| # | Run | Params | Status | Score |\n"
        runs_section += "|---|-----|--------|--------|-------|\n"
        for i, r in enumerate(runs, 1):
            rid = r.get("run_id", "")
            short = rid[:8] if rid else ""
            params_str = ", ".join(f"{k}={v}" for k, v in (r.get("params") or {}).items()) or "—"
            score_str = f"{r.get('score', 0)}" if r.get("score") is not None else "—"
            runs_section += f"| {i} | [[runs/{rid}|{short}]] | {params_str} | {r.get('status', '?')} | {score_str} |\n"
    else:
        runs_section += "_(no runs yet)_\n"
    runs_section += "\n"

    template = campaign.get("template", {}) or {}
    template_section = "## Template\n\n"
    template_section += f"- **Project**: `{template.get('project_name', '')}`\n"
    template_section += f"- **Target**: `{template.get('deploy_target', '')}`\n"
    template_section += f"- **Planner**: `{template.get('planner_model', '')}`\n"
    template_section += f"- **Generators**: {', '.join(f'`{m}`' for m in template.get('generator_models', []))}\n"
    template_section += f"- **Judge**: `{template.get('judge_model', '')}`\n\n"

    content = front_matter + overview + params_section + template_section + runs_section

    vault_write_local("campaigns", filename, content)
    vault_sync_file("campaigns", filename, campaign_id)
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
        content += f"- `{ts}` `{rid}` score={s} {ok} [[models/{_vault_safe_name(model)}|{model}]] — {prompt_preview}\n"

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

    # we don't track target in prompt_index, so we pull from RUN_STATUS
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


@task(name="vault_after_run", retries=2)
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


