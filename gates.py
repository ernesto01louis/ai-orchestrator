"""
Gates — Self-Learning Pre-Execution Safety Layer

Runs before every tool call and SSH command. Checks against learned rules,
logs incidents as lessons, and auto-promotes frequent mistakes into permanent
blocking gates.

Architecture:
  - gates.json         : permanent blocking rules (auto-promoted + manual)
  - memory/lessons/    : individual incident files (one per lesson)
  - Middleware hooks    : check_gate() called before execute_tool() and ssh_command()

Flow:
  1. Pre-execution:  check_gate(command, tool_name, args, run_id) -> allow/block
  2. On incident:    record_lesson(command, reason, context, run_id, source)
  3. Consolidation:  consolidate_lessons() -> auto-promote frequent patterns
"""

import fcntl
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path


GATES_FILE = "/opt/ai-orchestrator/gates.json"
LESSONS_DIR = "/opt/ai-orchestrator/memory/lessons"
GATES_LOG = "/opt/ai-orchestrator/memory/gates_log.json"

# Auto-promotion threshold: if a lesson pattern appears this many times, it becomes a gate
AUTO_PROMOTE_THRESHOLD = 3

# Maximum lessons to keep before forcing consolidation
MAX_LESSONS = 500

# Maximum gate log entries
MAX_GATE_LOG = 100


def _ensure_dirs():
    Path(LESSONS_DIR).mkdir(parents=True, exist_ok=True)


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


# ------------------------------------------------
# GATE RULES
# ------------------------------------------------

def load_gates():
    """Load all gate rules from gates.json."""
    return _load_json(GATES_FILE, {"gates": [], "version": 1})


def save_gates(data):
    _save_json(GATES_FILE, data)


def add_gate(pattern, reason, source="manual", severity="block"):
    """
    Add a new gate rule.

    Args:
        pattern: regex pattern to match against commands
        reason: why this pattern is blocked
        source: 'manual' or 'auto-promoted'
        severity: 'block' (hard stop) or 'warn' (log but allow)
    """
    data = load_gates()

    # Don't add duplicate patterns
    for gate in data["gates"]:
        if gate["pattern"] == pattern:
            return gate

    gate = {
        "id": f"gate-{int(time.time())}-{len(data['gates'])}",
        "pattern": pattern,
        "reason": reason,
        "source": source,
        "severity": severity,
        "created": datetime.utcnow().isoformat(),
        "trigger_count": 0,
        "last_triggered": None,
        "enabled": True,
    }
    data["gates"].append(gate)
    save_gates(data)
    return gate


def remove_gate(gate_id):
    """Remove a gate rule by ID."""
    data = load_gates()
    data["gates"] = [g for g in data["gates"] if g["id"] != gate_id]
    save_gates(data)


def toggle_gate(gate_id, enabled=True):
    """Enable or disable a gate rule."""
    data = load_gates()
    for gate in data["gates"]:
        if gate["id"] == gate_id:
            gate["enabled"] = enabled
            break
    save_gates(data)


# ------------------------------------------------
# PRE-EXECUTION CHECK
# ------------------------------------------------

def check_gate(command, tool_name="", args=None, run_id=""):
    """
    Check if a command should be blocked by any gate rule.

    Returns:
        (allowed: bool, gate: dict or None, message: str)
    """
    data = load_gates()

    # Build the full context string to check against
    check_str = command
    if tool_name:
        check_str = f"[{tool_name}] {command}"
    if args:
        check_str += f" args={json.dumps(args)}"

    for gate in data["gates"]:
        if not gate.get("enabled", True):
            continue

        try:
            if re.search(gate["pattern"], check_str, re.IGNORECASE):
                # Update trigger stats
                gate["trigger_count"] = gate.get("trigger_count", 0) + 1
                gate["last_triggered"] = datetime.utcnow().isoformat()
                save_gates(data)

                severity = gate.get("severity", "block")
                msg = f"Gate [{gate['id']}]: {gate['reason']} (pattern: {gate['pattern']})"

                if severity == "block":
                    # Record the block as a lesson too
                    record_lesson(
                        command=command,
                        reason=f"Blocked by gate: {gate['reason']}",
                        context={"tool": tool_name, "args": args, "gate_id": gate["id"]},
                        run_id=run_id,
                        source="gate_block",
                    )
                    return False, gate, msg
                else:
                    # Warn but allow
                    record_lesson(
                        command=command,
                        reason=f"Warning from gate: {gate['reason']}",
                        context={"tool": tool_name, "args": args, "gate_id": gate["id"]},
                        run_id=run_id,
                        source="gate_warn",
                    )
                    return True, gate, f"[WARN] {msg}"

        except re.error:
            # Invalid regex in gate — skip it
            continue

    return True, None, ""


# ------------------------------------------------
# LESSON RECORDING
# ------------------------------------------------

def record_lesson(command, reason, context=None, run_id="", source="system"):
    """
    Record a single lesson (incident) for later consolidation.

    Args:
        command: the command or action that triggered the lesson
        reason: why it was problematic
        context: dict with extra info (tool name, args, error output, etc.)
        run_id: the orchestrator run that produced this
        source: 'gate_block', 'gate_warn', 'runtime_failure', 'manual', 'system'
    """
    _ensure_dirs()

    lesson = {
        "timestamp": datetime.utcnow().isoformat(),
        "command": command[:500],
        "reason": reason[:500],
        "context": context or {},
        "run_id": run_id,
        "source": source,
        "pattern": _extract_pattern(command),
    }

    # Write individual lesson file
    filename = f"lesson-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.json"
    filepath = os.path.join(LESSONS_DIR, filename)
    _save_json(filepath, lesson)

    return lesson


def _extract_pattern(command):
    """
    Extract a generalizable pattern from a specific command.
    Replaces specific paths, IPs, ports, filenames with wildcards.
    """
    pattern = command

    # Normalize paths (keep the command, replace specific paths)
    pattern = re.sub(r"/[\w/.-]+\.\w+", " <path>", pattern)

    # Normalize IPs
    pattern = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "<ip>", pattern)

    # Normalize port numbers
    pattern = re.sub(r":\d{2,5}\b", ":<port>", pattern)

    # Normalize UUIDs
    pattern = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", pattern)

    # Collapse whitespace
    pattern = re.sub(r"\s+", " ", pattern).strip()

    return pattern


# ------------------------------------------------
# LESSON CONSOLIDATION
# ------------------------------------------------

def load_all_lessons():
    """Load all lesson files from the lessons directory."""
    _ensure_dirs()
    lessons = []
    for fname in sorted(os.listdir(LESSONS_DIR)):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(LESSONS_DIR, fname)) as f:
                    lesson = json.load(f)
                    lesson["_filename"] = fname
                    lessons.append(lesson)
            except (json.JSONDecodeError, OSError):
                continue
    return lessons


def consolidate_lessons(dry_run=False, log_fn=None):
    """
    Scan all lessons, count pattern frequency, and auto-promote
    patterns that appear >= AUTO_PROMOTE_THRESHOLD times into gates.

    Args:
        dry_run: if True, report what would be promoted but don't do it
        log_fn: optional logging function

    Returns:
        dict with consolidation report
    """
    lessons = load_all_lessons()
    if not lessons:
        return {
            "total_lessons": 0,
            "unique_patterns": 0,
            "promoted": [],
            "promoted_count": 0,
            "cleaned_lessons": 0,
            "timestamp": datetime.utcnow().isoformat(),
            "dry_run": dry_run,
        }

    # Count patterns
    pattern_counts = {}
    pattern_reasons = {}
    pattern_lessons = {}

    for lesson in lessons:
        pattern = lesson.get("pattern", "")
        if not pattern or lesson.get("source") in ("gate_block", "gate_warn"):
            # Don't count gate-triggered lessons toward promotion
            continue

        if pattern not in pattern_counts:
            pattern_counts[pattern] = 0
            pattern_reasons[pattern] = []
            pattern_lessons[pattern] = []

        pattern_counts[pattern] += 1
        reason = lesson.get("reason", "")
        if reason and reason not in pattern_reasons[pattern]:
            pattern_reasons[pattern].append(reason)
        pattern_lessons[pattern].append(lesson.get("_filename", ""))

    # Find patterns that meet the threshold
    promotable = {
        p: count for p, count in pattern_counts.items()
        if count >= AUTO_PROMOTE_THRESHOLD
    }

    promoted = []
    existing_gates = load_gates()
    existing_patterns = {g["pattern"] for g in existing_gates.get("gates", [])}

    for pattern, count in promotable.items():
        # Convert the generalized pattern into a regex
        regex = _pattern_to_regex(pattern)

        if regex in existing_patterns:
            continue

        reasons = pattern_reasons.get(pattern, ["repeated failure pattern"])
        combined_reason = f"Auto-promoted ({count} occurrences): {'; '.join(reasons[:3])}"

        if not dry_run:
            gate = add_gate(
                pattern=regex,
                reason=combined_reason,
                source="auto-promoted",
                severity="warn",  # start as warn, user can escalate to block
            )
            promoted.append({
                "pattern": pattern,
                "regex": regex,
                "count": count,
                "gate_id": gate["id"],
                "reasons": reasons[:3]
            })

            if log_fn:
                log_fn("gates", f"auto-promoted gate: {regex} ({count} occurrences)")
        else:
            promoted.append({
                "pattern": pattern,
                "regex": regex,
                "count": count,
                "reasons": reasons[:3],
                "would_promote": True
            })

    # Clean up old lesson files (keep only recent ones)
    cleaned = 0
    if not dry_run and len(lessons) > MAX_LESSONS:
        # Sort by timestamp, remove oldest
        lessons_sorted = sorted(lessons, key=lambda l: l.get("timestamp", ""))
        to_remove = lessons_sorted[:len(lessons) - MAX_LESSONS]
        for lesson in to_remove:
            fname = lesson.get("_filename", "")
            if fname:
                try:
                    os.remove(os.path.join(LESSONS_DIR, fname))
                    cleaned += 1
                except OSError:
                    pass

    report = {
        "total_lessons": len(lessons),
        "unique_patterns": len(pattern_counts),
        "promoted": promoted,
        "promoted_count": len(promoted),
        "cleaned_lessons": cleaned,
        "timestamp": datetime.utcnow().isoformat(),
        "dry_run": dry_run,
    }

    # Log the consolidation
    if not dry_run:
        gate_log = _load_json(GATES_LOG, [])
        gate_log.append(report)
        if len(gate_log) > MAX_GATE_LOG:
            gate_log = gate_log[-MAX_GATE_LOG:]
        _save_json(GATES_LOG, gate_log)

    return report


def _pattern_to_regex(pattern):
    """
    Convert a generalized pattern (with <path>, <ip>, etc.) into a regex.
    """
    # Escape regex special chars in the literal parts
    escaped = re.escape(pattern)

    # Replace our placeholders with regex groups
    escaped = escaped.replace(re.escape("<path>"), r"[\w/.\-]+\.\w+")
    escaped = escaped.replace(re.escape("<ip>"), r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
    escaped = escaped.replace(re.escape("<port>"), r":\d{2,5}")
    escaped = escaped.replace(re.escape("<uuid>"), r"[0-9a-f\-]{36}")

    return escaped


# ------------------------------------------------
# RUNTIME FAILURE HOOK
# ------------------------------------------------

def record_runtime_failure(command, error_output, tool_name="", run_id=""):
    """
    Called when a tool execution or SSH command fails at runtime.
    Records it as a lesson so repeated failures get auto-promoted.
    """
    return record_lesson(
        command=command,
        reason=f"Runtime failure: {error_output[:300]}",
        context={
            "tool": tool_name,
            "error": error_output[:500],
        },
        run_id=run_id,
        source="runtime_failure",
    )


# ------------------------------------------------
# API HELPERS
# ------------------------------------------------

def get_gates_summary():
    """Get a summary of all gates for API response."""
    data = load_gates()
    gates = data.get("gates", [])
    return {
        "total": len(gates),
        "enabled": sum(1 for g in gates if g.get("enabled", True)),
        "auto_promoted": sum(1 for g in gates if g.get("source") == "auto-promoted"),
        "manual": sum(1 for g in gates if g.get("source") == "manual"),
        "total_triggers": sum(g.get("trigger_count", 0) for g in gates),
        "gates": gates,
    }


def get_lessons_summary():
    """Get a summary of lessons for API response."""
    lessons = load_all_lessons()
    sources = {}
    for l in lessons:
        src = l.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1

    return {
        "total": len(lessons),
        "by_source": sources,
        "recent": lessons[-10:] if lessons else [],
    }


# Initialize gates.json if it doesn't exist
def init_gates():
    """Create gates.json with default rules if it doesn't exist."""
    _ensure_dirs()
    if not os.path.exists(GATES_FILE):
        _save_json(GATES_FILE, {
            "gates": [],
            "version": 1,
            "created": datetime.utcnow().isoformat(),
        })
