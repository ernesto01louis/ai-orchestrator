"""
Dream — Memory Consolidation Engine

Periodically cleans, deduplicates, and consolidates the orchestrator's memory layers.
Inspired by sleep-cycle memory consolidation: merge what's useful, discard what's stale,
resolve contradictions, and produce a health report.

Can be triggered via:
  - POST /dream           (API endpoint)
  - After every N runs     (auto-trigger, configurable)
  - Cron job               (external scheduler)
"""

import fcntl
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path


try:
    from core.paths import MEMORY_DIR, DREAM_LOG, VAULT_DIR_DEFAULT as VAULT_DIR  # type: ignore
except ImportError:  # standalone use without core/ on path
    MEMORY_DIR = "/opt/ai-orchestrator/memory"
    VAULT_DIR = "/opt/ai-orchestrator/vault"
    DREAM_LOG = Path(MEMORY_DIR) / "dream_log.json"

# Consolidation thresholds
DUPLICATE_COSINE_THRESHOLD = 0.95   # prompts this similar are considered duplicates
STALE_DAYS_LOW_SCORE = 60           # entries older than this with score < 7 get pruned
STALE_DAYS_NEGATIVE = 90            # negative memories older than this get pruned (if superseded)
SESSION_COLLAPSE_DAYS = 7           # sessions older than this get collapsed to summaries
MAX_RECENT_SCORES = 20              # trim model_stats recent_scores arrays
MAX_DREAM_LOG_ENTRIES = 50          # rolling dream log history


def _load_json(path, default):
    try:
        with open(path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
            return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return default
    except OSError:
        return default


def _save_json(path, data):
    try:
        with open(path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                json.dump(data, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        with open(path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _parse_timestamp(ts):
    """Parse ISO timestamp string, return datetime or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00", ""))
    except (ValueError, AttributeError):
        return None


def _normalize_timestamp(ts):
    """Ensure timestamp is absolute ISO format."""
    dt = _parse_timestamp(ts)
    if dt:
        return dt.isoformat()
    return ts


# ------------------------------------------------
# CONSOLIDATION PASSES
# ------------------------------------------------

def consolidate_prompt_index(now):
    """
    Deduplicate near-identical prompts (keep highest score).
    Prune old low-scoring entries.
    Normalize timestamps.
    """
    path = Path(MEMORY_DIR) / "prompt_index.json"
    index = _load_json(path, [])
    if not index:
        return {"prompt_index": "empty, skipped"}

    original_count = len(index)
    actions = []
    cutoff = now - timedelta(days=STALE_DAYS_LOW_SCORE)

    # Pass 1: normalize timestamps
    for entry in index:
        if "timestamp" in entry:
            entry["timestamp"] = _normalize_timestamp(entry["timestamp"])

    # Pass 2: prune old low-scoring entries
    pruned = []
    for entry in index:
        ts = _parse_timestamp(entry.get("timestamp"))
        score = entry.get("score", 0)
        if ts and ts < cutoff and score < 7:
            actions.append(f"pruned stale entry: score={score}, project={entry.get('project', '?')}, age={( now - ts).days}d")
            continue
        pruned.append(entry)

    # Pass 3: deduplicate by embedding similarity (keep highest score)
    # Guard: skip dedup if too many entries (O(n²) comparison)
    if len(pruned) > 500:
        actions.append(f"skipped deduplication ({len(pruned)} entries > 500 threshold)")
        _save_json(path, pruned)
        return {
            "prompt_index": {
                "before": original_count,
                "after": len(pruned),
                "pruned": original_count - len(pruned),
                "merged": 0,
                "actions": actions
            }
        }

    deduplicated = []
    skip_indices = set()

    for i, entry_a in enumerate(pruned):
        if i in skip_indices:
            continue
        emb_a = entry_a.get("embedding")
        if not emb_a:
            deduplicated.append(entry_a)
            continue

        best = entry_a
        merged_ids = []

        for j in range(i + 1, len(pruned)):
            if j in skip_indices:
                continue
            emb_b = pruned[j].get("embedding")
            if not emb_b:
                continue
            sim = _cosine_similarity(emb_a, emb_b)
            if sim >= DUPLICATE_COSINE_THRESHOLD:
                skip_indices.add(j)
                merged_ids.append(pruned[j].get("run_id", "?"))
                # keep the higher-scoring version
                if pruned[j].get("score", 0) > best.get("score", 0):
                    best = pruned[j]

        if merged_ids:
            actions.append(f"merged {len(merged_ids)+1} similar entries -> kept run_id={best.get('run_id', '?')} (score={best.get('score', 0)})")

        deduplicated.append(best)

    _save_json(path, deduplicated)

    return {
        "prompt_index": {
            "before": original_count,
            "after": len(deduplicated),
            "pruned": original_count - len(pruned),
            "merged": len(pruned) - len(deduplicated),
            "actions": actions
        }
    }


def consolidate_negative_memory(now, prompt_index):
    """
    Remove negative entries where a later successful run exists for the same prompt.
    Prune very old entries.
    Merge failures with identical error patterns.
    """
    path = Path(MEMORY_DIR) / "negative_memory.json"
    entries = _load_json(path, [])
    if not entries:
        return {"negative_memory": "empty, skipped"}

    original_count = len(entries)
    actions = []
    cutoff = now - timedelta(days=STALE_DAYS_NEGATIVE)

    # Build set of successful prompt embeddings for supersession check
    success_embeddings = []
    for p in prompt_index:
        if p.get("success") and p.get("embedding"):
            success_embeddings.append(p["embedding"])

    filtered = []
    for entry in entries:
        ts = _parse_timestamp(entry.get("timestamp"))
        emb = entry.get("embedding")

        # Normalize timestamp
        if "timestamp" in entry:
            entry["timestamp"] = _normalize_timestamp(entry["timestamp"])

        # Check if this failure was superseded by a later success
        superseded = False
        if emb and success_embeddings:
            for s_emb in success_embeddings:
                if _cosine_similarity(emb, s_emb) >= DUPLICATE_COSINE_THRESHOLD:
                    superseded = True
                    break

        if superseded:
            actions.append(f"removed superseded failure: project={entry.get('project', '?')}")
            continue

        # Prune very old entries
        if ts and ts < cutoff:
            actions.append(f"pruned stale failure: project={entry.get('project', '?')}, age={(now - ts).days}d")
            continue

        filtered.append(entry)

    # Merge failures with identical error summaries
    merged = []
    skip_indices = set()
    for i, entry_a in enumerate(filtered):
        if i in skip_indices:
            continue
        err_a = entry_a.get("error_summary", "")
        if not err_a:
            merged.append(entry_a)
            continue

        count = 1
        for j in range(i + 1, len(filtered)):
            if j in skip_indices:
                continue
            err_b = filtered[j].get("error_summary", "")
            if err_a == err_b:
                skip_indices.add(j)
                count += 1

        if count > 1:
            actions.append(f"merged {count} failures with same error: {err_a[:80]}")
        merged.append(entry_a)

    _save_json(path, merged)

    return {
        "negative_memory": {
            "before": original_count,
            "after": len(merged),
            "superseded": original_count - len(filtered),
            "merged": len(filtered) - len(merged),
            "actions": actions
        }
    }


def consolidate_embedding_cache(prompt_index, negative_memory):
    """
    Remove orphaned embeddings not referenced by prompt_index or negative_memory.
    """
    path = Path(MEMORY_DIR) / "embedding_cache.json"
    cache = _load_json(path, {})
    if not cache:
        return {"embedding_cache": "empty, skipped"}

    original_count = len(cache)

    # Collect all prompts that are still referenced
    referenced_prompts = set()
    for entry in prompt_index:
        if "prompt" in entry:
            referenced_prompts.add(entry["prompt"])
    for entry in negative_memory:
        if "prompt" in entry:
            referenced_prompts.add(entry["prompt"])

    # Keep only referenced embeddings
    cleaned = {k: v for k, v in cache.items() if k in referenced_prompts}
    evicted = original_count - len(cleaned)

    _save_json(path, cleaned)

    return {
        "embedding_cache": {
            "before": original_count,
            "after": len(cleaned),
            "evicted": evicted
        }
    }


def consolidate_session_log(now):
    """
    Collapse old sessions into summary entries (keep run count, score avg, date range).
    Keep recent sessions detailed.
    """
    path = Path(MEMORY_DIR) / "session_log.json"
    sessions = _load_json(path, [])
    if not sessions:
        return {"session_log": "empty, skipped"}

    original_count = len(sessions)
    cutoff = now - timedelta(days=SESSION_COLLAPSE_DAYS)
    actions = []

    consolidated = []
    for session in sessions:
        last_activity = _parse_timestamp(session.get("last_activity") or session.get("started"))

        if last_activity and last_activity < cutoff:
            # Collapse to summary
            runs = session.get("runs", [])
            scores = [r.get("score", 0) for r in runs if isinstance(r.get("score"), (int, float))]
            avg_score = sum(scores) / len(scores) if scores else 0
            successes = sum(1 for r in runs if r.get("success"))

            summary = {
                "session_id": session.get("session_id"),
                "started": session.get("started"),
                "last_activity": session.get("last_activity"),
                "run_count": len(runs),
                "avg_score": round(avg_score, 1),
                "successes": successes,
                "collapsed": True,
                "runs": []  # clear detailed run data
            }
            consolidated.append(summary)
            actions.append(f"collapsed session {session.get('session_id', '?')}: {len(runs)} runs -> summary")
        else:
            consolidated.append(session)

    _save_json(path, consolidated)

    return {
        "session_log": {
            "before": original_count,
            "collapsed": len(actions),
            "actions": actions
        }
    }


def consolidate_model_stats(available_models=None):
    """
    Prune models no longer available.
    Trim recent_scores to MAX_RECENT_SCORES.
    """
    path = Path(MEMORY_DIR) / "model_stats.json"
    stats = _load_json(path, {})
    if not stats:
        return {"model_stats": "empty, skipped"}

    actions = []
    pruned_models = []

    # Prune unavailable models (only if we have a list)
    if available_models is not None:
        for model in list(stats.keys()):
            if model not in available_models:
                pruned_models.append(model)
                del stats[model]
                actions.append(f"pruned unavailable model: {model}")

    # Trim recent_scores arrays
    for model, s in stats.items():
        scores = s.get("recent_scores", [])
        if len(scores) > MAX_RECENT_SCORES:
            trimmed = len(scores) - MAX_RECENT_SCORES
            s["recent_scores"] = scores[-MAX_RECENT_SCORES:]
            actions.append(f"trimmed {trimmed} old scores from {model}")

    _save_json(path, stats)

    return {
        "model_stats": {
            "models": len(stats),
            "pruned": len(pruned_models),
            "actions": actions
        }
    }


def audit_vault():
    """
    Scan vault for orphaned run notes and stale daily digests.
    Returns advisory list (doesn't delete — vault is user-facing).
    """
    runs_dir = Path(VAULT_DIR) / "runs"
    daily_dir = Path(VAULT_DIR) / "daily"
    session_log = _load_json(Path(MEMORY_DIR) / "session_log.json", [])
    advisories = []

    # Collect all known run_ids from session_log
    known_run_ids = set()
    for session in session_log:
        for run in session.get("runs", []):
            rid = run.get("run_id", "")
            if rid:
                known_run_ids.add(rid[:8])  # vault filenames use first 8 chars

    # Check for orphaned run notes
    if runs_dir.exists():
        for f in runs_dir.iterdir():
            if f.suffix == ".md":
                # extract run_id fragment from filename
                parts = f.stem.split("_")
                if len(parts) >= 2:
                    rid_fragment = parts[-1]
                    if rid_fragment not in known_run_ids and len(known_run_ids) > 0:
                        advisories.append(f"possibly orphaned: vault/runs/{f.name}")

    # Check for old daily digests (>30 days)
    now = datetime.utcnow()
    if daily_dir.exists():
        for f in daily_dir.iterdir():
            if f.suffix == ".md":
                try:
                    file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                    if (now - file_date).days > 30:
                        advisories.append(f"stale daily digest (>{30}d): vault/daily/{f.name}")
                except ValueError:
                    pass

    return {"vault_audit": {"advisories": advisories, "count": len(advisories)}}


def compute_health_score(results):
    """
    Compute an overall memory health score (0-100) based on consolidation results.
    Higher = healthier (less clutter, fewer stale entries, clean cache).
    """
    score = 100
    deductions = []

    # Prompt index: deduct for high merge/prune ratios
    pi = results.get("prompt_index", {})
    if isinstance(pi, dict) and pi.get("before", 0) > 0:
        prune_ratio = (pi.get("pruned", 0) + pi.get("merged", 0)) / pi["before"]
        if prune_ratio > 0.3:
            d = int(prune_ratio * 20)
            score -= d
            deductions.append(f"prompt_index clutter: -{d}")

    # Embedding cache: deduct for high orphan ratio
    ec = results.get("embedding_cache", {})
    if isinstance(ec, dict) and ec.get("before", 0) > 0:
        orphan_ratio = ec.get("evicted", 0) / ec["before"]
        if orphan_ratio > 0.2:
            d = int(orphan_ratio * 15)
            score -= d
            deductions.append(f"embedding orphans: -{d}")

    # Negative memory: deduct for many superseded entries
    nm = results.get("negative_memory", {})
    if isinstance(nm, dict) and nm.get("before", 0) > 0:
        if nm.get("superseded", 0) > 3:
            d = min(10, nm["superseded"] * 2)
            score -= d
            deductions.append(f"superseded failures: -{d}")

    # Vault: deduct for advisories
    va = results.get("vault_audit", {})
    if isinstance(va, dict):
        advisory_count = va.get("count", 0)
        if advisory_count > 5:
            d = min(10, advisory_count)
            score -= d
            deductions.append(f"vault advisories: -{d}")

    return max(0, min(100, score)), deductions


# ------------------------------------------------
# MAIN DREAM ENTRY POINT
# ------------------------------------------------

def run_dream(available_models=None, log_fn=None):
    """
    Execute a full dream cycle — consolidate all memory layers.

    Args:
        available_models: set of model names currently loaded (optional, for pruning)
        log_fn: optional logging function (e.g. app.log)

    Returns:
        dict with full consolidation report
    """
    now = datetime.utcnow()
    started = time.time()

    if log_fn:
        log_fn("dream", "dream cycle starting")

    results = {}

    # 1. Prompt index (load fresh for cross-referencing)
    results.update(consolidate_prompt_index(now))
    prompt_index = _load_json(Path(MEMORY_DIR) / "prompt_index.json", [])

    # 2. Negative memory (needs prompt_index for supersession check)
    results.update(consolidate_negative_memory(now, prompt_index))
    negative_memory = _load_json(Path(MEMORY_DIR) / "negative_memory.json", [])

    # 3. Embedding cache (needs both indexes for orphan detection)
    results.update(consolidate_embedding_cache(prompt_index, negative_memory))

    # 4. Session log
    results.update(consolidate_session_log(now))

    # 5. Model stats
    results.update(consolidate_model_stats(available_models))

    # 6. Vault audit (advisory only)
    results.update(audit_vault())

    # 7. Health score
    health_score, deductions = compute_health_score(results)
    results["health"] = {
        "score": health_score,
        "deductions": deductions,
        "rating": "excellent" if health_score >= 90 else "good" if health_score >= 70 else "fair" if health_score >= 50 else "poor"
    }

    elapsed = round(time.time() - started, 2)
    results["meta"] = {
        "timestamp": now.isoformat(),
        "elapsed_seconds": elapsed
    }

    # Append to dream log
    dream_log = _load_json(DREAM_LOG, [])
    dream_log.append({
        "timestamp": now.isoformat(),
        "health_score": health_score,
        "health_rating": results["health"]["rating"],
        "elapsed": elapsed,
        "summary": {
            k: v.get("after", v.get("count", "n/a")) if isinstance(v, dict) else v
            for k, v in results.items()
            if k not in ("meta", "health")
        }
    })
    if len(dream_log) > MAX_DREAM_LOG_ENTRIES:
        dream_log = dream_log[-MAX_DREAM_LOG_ENTRIES:]
    _save_json(DREAM_LOG, dream_log)

    if log_fn:
        log_fn("dream", f"dream cycle complete: health={health_score}/100 ({results['health']['rating']}), took {elapsed}s")

    return results
