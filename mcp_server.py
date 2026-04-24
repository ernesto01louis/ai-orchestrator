"""
MCP Server — Exposes the orchestrator's capabilities via Model Context Protocol.

Mounted into the main FastAPI app at /mcp, allowing MCP clients
(Claude Desktop, VS Code, other AI tools) to:
  - Trigger orchestration runs
  - Query memory, agents, and run status
  - Manage gates and dream cycles
  - Read agent prompts and system state

Transport: Streamable HTTP (mounted as ASGI sub-app)
"""

import json
import uuid
import threading

from mcp.server.fastmcp import FastMCP


# Create MCP server instance
mcp = FastMCP(
    "AI Orchestrator",
    instructions=(
        "You are connected to an autonomous AI code orchestrator. "
        "It generates, tests, judges, optimizes, and deploys code to Raspberry Pi targets "
        "using local Ollama models. Use the available tools to submit tasks, check status, "
        "query memory, and manage the system."
    ),
)


# ------------------------------------------------
# Late-binding helpers: import app internals at call time
# to avoid circular imports (mcp_server is imported by app.py)
# ------------------------------------------------

def _app():
    import app as _a
    return _a


# ------------------------------------------------
# TOOLS: Actions that modify state or trigger work
# ------------------------------------------------

@mcp.tool()
def orchestrate(
    project_name: str,
    prompt: str,
    deploy_target: str,
    planner_model: str = "qwen2.5:72b",
    generator_models: str = "qwen2.5-coder:32b",
    judge_model: str = "qwen2.5:72b",
    optimizer_model: str = "",
    troubleshooter_model: str = "",
    max_iterations: int = 3,
) -> dict:
    """Submit an orchestration job. Generates, tests, judges, and deploys code to a target.

    Args:
        project_name: Name for the project (used as deploy directory name)
        prompt: What to build — describe the task in natural language
        deploy_target: Target device name (e.g. 'pi-1', 'pi-2', 'Rak')
        planner_model: Model for planning (default: qwen2.5:72b)
        generator_models: Comma-separated model names for code generation
        judge_model: Model for code review/scoring
        optimizer_model: Model for optimization pass (optional, leave empty to skip)
        troubleshooter_model: Model for fixing runtime errors (optional)
        max_iterations: Max generate-judge-optimize loops (default: 3)
    """
    a = _app()

    try:
        a.validate_target(deploy_target)
    except ValueError as e:
        return {"error": str(e)}

    gen_models = [m.strip() for m in generator_models.split(",") if m.strip()]

    req = a.OrchestrateRequest(
        project_name=project_name,
        prompt=prompt,
        planner_model=planner_model,
        generator_models=gen_models,
        judge_model=judge_model,
        optimizer_model=optimizer_model or None,
        troubleshooter_model=troubleshooter_model or None,
        max_iterations=max_iterations,
        deploy_target=deploy_target,
    )

    run_id = str(uuid.uuid4())
    a._init_run_status(run_id, project=project_name, target=deploy_target)

    thread = threading.Thread(target=a.run_orchestration, args=(req, run_id), daemon=True)
    thread.start()

    return {
        "run_id": run_id,
        "status": "started",
        "message": f"Orchestration started for '{project_name}' on {deploy_target}. Use get_run_status to poll progress.",
    }


@mcp.tool()
def get_run_status(run_id: str) -> dict:
    """Check the status of an orchestration run.

    Args:
        run_id: The run ID returned by the orchestrate tool
    """
    a = _app()
    status = a.RUN_STATUS.get(run_id)
    if not status:
        return {"error": f"Run {run_id} not found"}
    # Filter out internal keys
    return {k: v for k, v in status.items() if not k.startswith("_")}


@mcp.tool()
def get_run_result(run_id: str) -> dict:
    """Get the final result of a completed orchestration run, including generated files and scores.

    Args:
        run_id: The run ID to get results for
    """
    a = _app()
    status = a.RUN_STATUS.get(run_id)
    if not status:
        return {"error": f"Run {run_id} not found"}
    if not status.get("completed"):
        return {"status": "still running", "phase": status.get("phase", "unknown")}
    if "error" in status:
        return {"error": status["error"]}
    return status.get("result", {})


@mcp.tool()
def list_targets() -> dict:
    """List all available deploy targets (Raspberry Pi devices) with their SSH details."""
    a = _app()
    targets = []
    for name, cfg in a.SSH_TARGETS.items():
        targets.append({
            "name": name,
            "host": cfg["host"],
            "username": cfg["username"],
        })
    return {"targets": targets}


@mcp.tool()
def list_models() -> dict:
    """List all Ollama models currently available across all LXC containers."""
    a = _app()
    a._refresh_url_cache()
    models = {}
    for model, base_url in a._url_cache.items():
        models[model] = {"server": base_url}
    return {"models": models}


@mcp.tool()
def run_dream_cycle() -> dict:
    """Trigger a Dream memory consolidation cycle. Cleans, deduplicates, and
    optimizes the orchestrator's memory layers. Returns a health report."""
    from dream import run_dream
    a = _app()
    available = set(a._url_cache.keys()) if a._url_cache else None
    return run_dream(available_models=available, log_fn=a.log)


@mcp.tool()
def add_safety_gate(pattern: str, reason: str, severity: str = "block") -> dict:
    """Add a manual safety gate rule that blocks or warns on matching commands.

    Args:
        pattern: Regex pattern to match against tool commands
        reason: Why this pattern should be blocked
        severity: 'block' (hard stop) or 'warn' (log but allow)
    """
    from gates import add_gate
    gate = add_gate(pattern, reason, source="manual", severity=severity)
    return gate


@mcp.tool()
def reload_agents() -> dict:
    """Reload all agent configurations from the agents/ folder.
    Use after editing prompt templates or agent.yaml files."""
    from agents.loader import reload_all
    agents = reload_all()
    return {"reloaded": list(agents.keys())}


@mcp.tool()
def update_agent_prompt(role: str, prompt_type: str, content: str) -> dict:
    """Update a prompt template for an agent role. Changes take effect on next run.

    Args:
        role: Agent role name (planner, generator, judge, optimizer, troubleshooter, tool_dispatch)
        prompt_type: Which prompt to update (system_prompt, user_prompt, user_prompt_multi)
        content: New prompt template content (supports {{variable}} placeholders)
    """
    import os
    from agents.loader import load_agent

    valid_types = {"system_prompt", "user_prompt", "user_prompt_multi"}
    if prompt_type not in valid_types:
        return {"error": f"prompt_type must be one of: {valid_types}"}

    try:
        agent = load_agent(role)
    except FileNotFoundError:
        return {"error": f"Agent role '{role}' not found"}

    prompt_path = os.path.join(agent.dir, f"{prompt_type}.md")
    with open(prompt_path, "w") as f:
        f.write(content)

    load_agent(role, force_reload=True)
    return {"updated": prompt_type, "role": role}


# ------------------------------------------------
# RESOURCES: Read-only data for context
# ------------------------------------------------

@mcp.resource("orchestrator://health")
def resource_health() -> str:
    """Current memory health score and system status."""
    from dream import DREAM_LOG, _load_json
    log_entries = _load_json(DREAM_LOG, [])
    latest = log_entries[-1] if log_entries else {}
    return json.dumps({
        "health_score": latest.get("health_score"),
        "health_rating": latest.get("health_rating", "unknown"),
        "last_dream": latest.get("timestamp"),
    }, indent=2)


@mcp.resource("orchestrator://identity")
def resource_identity() -> str:
    """The orchestrator's identity and core principles (Layer 1 memory)."""
    a = _app()
    return a.load_identity()


@mcp.resource("orchestrator://primer")
def resource_primer() -> str:
    """Current session state — active project, recent runs, blockers (Layer 2 memory)."""
    a = _app()
    try:
        return a.PRIMER_FILE.read_text()
    except FileNotFoundError:
        return "No primer available."


@mcp.resource("orchestrator://goals")
def resource_goals() -> str:
    """High-level goals and roadmap."""
    a = _app()
    try:
        return a.GOALS_FILE.read_text()
    except FileNotFoundError:
        return "No goals defined."


@mcp.resource("orchestrator://model-stats")
def resource_model_stats() -> str:
    """Per-model performance statistics — win rates, scores by language/role."""
    a = _app()
    stats = a.load_model_stats()
    return json.dumps(stats, indent=2)


@mcp.resource("orchestrator://agents")
def resource_agents() -> str:
    """All configured agent roles with their metadata."""
    from agents.loader import load_all_agents
    agents = load_all_agents()
    return json.dumps({role: cfg.to_dict() for role, cfg in agents.items()}, indent=2)


@mcp.resource("orchestrator://agents/{role}")
def resource_agent_detail(role: str) -> str:
    """Full agent config including prompt templates for a specific role."""
    from agents.loader import load_agent
    try:
        agent = load_agent(role)
    except FileNotFoundError:
        return json.dumps({"error": f"Agent role '{role}' not found"})

    return json.dumps({
        **agent.to_dict(),
        "system_prompt": agent.system_prompt_template,
        "user_prompt": agent.user_prompt_template,
        "user_prompt_multi": agent.user_prompt_multi_template,
        "schema": agent.schema,
        "variants": agent.variants,
    }, indent=2)


@mcp.resource("orchestrator://gates")
def resource_gates() -> str:
    """Current safety gate rules and trigger statistics."""
    from gates import get_gates_summary
    return json.dumps(get_gates_summary(), indent=2)


@mcp.resource("orchestrator://gates/lessons")
def resource_lessons() -> str:
    """Recent safety lessons and incident history."""
    from gates import get_lessons_summary
    return json.dumps(get_lessons_summary(), indent=2)


@mcp.resource("orchestrator://dream/log")
def resource_dream_log() -> str:
    """History of dream consolidation cycles with health scores."""
    from dream import DREAM_LOG, _load_json
    return json.dumps(_load_json(DREAM_LOG, []), indent=2)


# ------------------------------------------------
# PROMPTS: Reusable prompt templates for LLM interactions
# ------------------------------------------------

@mcp.prompt()
def plan_task(task: str, target: str = "pi-1") -> str:
    """Generate a structured implementation plan for a coding task.

    Args:
        task: Description of what to build
        target: Target device (default: pi-1)
    """
    return (
        f"You are an AI code orchestrator. Create an implementation plan for this task.\n\n"
        f"TARGET: {target}\n"
        f"TASK: {task}\n\n"
        f"Decide:\n"
        f"- Language: python, bash, or javascript\n"
        f"- Project type: script (run-and-exit) or server (long-running)\n"
        f"- Files needed and their responsibilities\n"
        f"- Dependencies required\n"
        f"- Execution mode: generate, tools_only, or tools_then_generate\n"
    )


@mcp.prompt()
def review_code(code: str, task: str) -> str:
    """Review code quality and suggest improvements.

    Args:
        code: The code to review
        task: What the code is supposed to do
    """
    return (
        f"Review this code for quality. Score each category 0-10:\n"
        f"- correctness, robustness, security, performance, structure, overall\n\n"
        f"TASK: {task}\n\n"
        f"CODE:\n{code}\n\n"
        f"List specific, actionable improvements."
    )


@mcp.prompt()
def troubleshoot_error(code: str, error: str, task: str) -> str:
    """Diagnose and fix a runtime error in generated code.

    Args:
        code: The failing code
        error: The error output
        task: What the code was supposed to do
    """
    return (
        f"This code crashed. Diagnose the issue and provide a fix.\n\n"
        f"TASK: {task}\n\n"
        f"ERROR:\n{error}\n\n"
        f"CODE:\n{code}\n\n"
        f"Explain what went wrong and provide the corrected code."
    )
