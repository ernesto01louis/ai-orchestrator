"""Tool registry + dispatcher.

Tools are defined in tool_registry.json (hot-reloadable). The dispatcher
asks `agents/tool_dispatch` which tools to run for a given prompt+plan,
then executes each via SSH on the target — gated by `check_gate` for
safety and recorded as a lesson on failure.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

from agents.loader import load_agent
from core.config import OLLAMA_PLANNER_CHAT
from core.runtime import log
from execution import deploy_file, ssh_command, sudo_command
from gates import check_gate, record_lesson, record_runtime_failure
from llm.ollama import query_ollama_structured, resolve_chat_url

# Loaded lazily to avoid circular import (orchestration owns the schemas)
def _tool_dispatch_schema():
    from orchestration import TOOL_DISPATCH_SCHEMA
    return TOOL_DISPATCH_SCHEMA


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
                # shell=True is required here: tool commands rely on shell
                # semantics (pipes, redirection, globbing). cmd is pre-screened
                # by _TOOL_CMD_BLOCKLIST above and runs under the orchestrator's
                # own user — see SECURITY.md T1 (defense-in-depth, not a
                # containment boundary).
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)  # nosec B602
                output = r.stdout.strip() or r.stderr.strip() or "[no output]"
                if r.returncode != 0:
                    record_runtime_failure(cmd, output[:300], tool_name, run_id)
                return output
            except (subprocess.SubprocessError, OSError) as e:
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
        _tool_dispatch_schema(),
        resolve_chat_url(planner_model),
        run_id,
        agent_role="tool_dispatch",
    ).parsed

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

