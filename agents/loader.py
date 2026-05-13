"""
Agent Loader — reads agent configs from the agents/ folder and renders prompts.

Each agent lives in agents/<role>/ with:
  - agent.yaml       : metadata (name, description, version, output_mode, etc.)
  - system_prompt.md  : system prompt template (optional)
  - user_prompt.md    : user prompt template
  - schema.json       : structured output schema (optional)
  - variants/         : language-specific overrides (optional)
"""

import json
import os
import re

import yaml

AGENTS_DIR = os.path.join(os.path.dirname(__file__))

# Cache: role -> loaded AgentConfig
_cache: dict[str, "AgentConfig"] = {}


class AgentConfig:
    """Loaded agent configuration with prompt templates and schema."""

    def __init__(self, role, agent_dir):
        self.role = role
        self.dir = agent_dir

        # load agent.yaml
        yaml_path = os.path.join(agent_dir, "agent.yaml")
        try:
            with open(yaml_path) as f:
                self.meta = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Malformed YAML in {yaml_path}: {e}")

        if not isinstance(self.meta, dict):
            raise ValueError(f"agent.yaml in {agent_dir} must be a YAML mapping, got {type(self.meta).__name__}")

        self.name = self.meta.get("name", role)
        self.description = self.meta.get("description", "")
        self.version = self.meta.get("version", 1)
        self.output_mode = self.meta.get("output_mode", "freeform")
        self.uses_identity = self.meta.get("identity", False)
        self.model_role = self.meta.get("defaults", {}).get("model_role", "")

        # load schema if structured
        self.schema = None
        schema_ref = self.meta.get("schema")
        if schema_ref:
            schema_path = os.path.join(agent_dir, schema_ref)
            if os.path.exists(schema_path):
                with open(schema_path) as f:
                    self.schema = json.load(f)

        # load prompt templates
        self.system_prompt_template = self._load_prompt("system_prompt")
        self.user_prompt_template = self._load_prompt("user_prompt")
        self.user_prompt_multi_template = self._load_prompt("user_prompt_multi")

        # load language variants
        self.variants = {}
        variants_dir = self.meta.get("variants_dir")
        if variants_dir:
            vdir = os.path.join(agent_dir, variants_dir)
            if os.path.isdir(vdir):
                for fname in os.listdir(vdir):
                    if fname.endswith((".yaml", ".yml")):
                        lang = os.path.splitext(fname)[0]
                        with open(os.path.join(vdir, fname)) as f:
                            self.variants[lang] = yaml.safe_load(f)

    def _load_prompt(self, key):
        """Load a prompt template file referenced in agent.yaml, or by convention."""
        ref = self.meta.get(key)
        if ref is None:
            # try convention: key + ".md"
            path = os.path.join(self.dir, f"{key}.md")
        elif ref in (False, "null", "none"):
            return None
        else:
            path = os.path.join(self.dir, ref)

        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
        return None

    def render(self, template, variables):
        """Render a prompt template with {{variable}} substitution."""
        if template is None:
            return None

        def replacer(match):
            key = match.group(1).strip()
            val = variables.get(key)
            if val is None:
                return ""
            return str(val)

        return re.sub(r"\{\{([\w\-\.]+)\}\}", replacer, template)

    def render_system_prompt(self, **variables):
        return self.render(self.system_prompt_template, variables)

    def render_user_prompt(self, multi=False, **variables):
        tpl = self.user_prompt_multi_template if multi and self.user_prompt_multi_template else self.user_prompt_template
        return self.render(tpl, variables)

    def get_variant(self, language):
        """Get language-specific overrides, or empty dict."""
        return self.variants.get(language, {})

    def to_dict(self):
        """Serialize for API responses."""
        return {
            "role": self.role,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "output_mode": self.output_mode,
            "identity": self.uses_identity,
            "model_role": self.model_role,
            "has_schema": self.schema is not None,
            "has_system_prompt": self.system_prompt_template is not None,
            "has_user_prompt": self.user_prompt_template is not None,
            "has_multi_prompt": self.user_prompt_multi_template is not None,
            "variants": list(self.variants.keys()),
            "dir": self.dir,
        }


def load_agent(role, force_reload=False):
    """Load an agent config by role name. Caches after first load."""
    if not force_reload and role in _cache:
        return _cache[role]

    agent_dir = os.path.join(AGENTS_DIR, role)
    if not os.path.isdir(agent_dir):
        raise FileNotFoundError(f"Agent role not found: {role} (looked in {agent_dir})")

    config_path = os.path.join(agent_dir, "agent.yaml")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Missing agent.yaml in {agent_dir}")

    agent = AgentConfig(role, agent_dir)
    _cache[role] = agent
    return agent


def load_schema(role):
    """Return the JSON schema dict for ``role`` from ``agents/<role>/schema.json``.

    Single source of truth for agent structured-output schemas. Replaces
    the inline-fallback pattern that previously lived in both
    ``app.py`` and ``orchestration/__init__.py`` — those copies drifted
    from the canonical files on disk (planner gained ``project_type``,
    judge swapped scalar ``score`` for multi-dimensional scoring, etc.),
    and silently activating the stale fallback when ``agents/`` was
    corrupted broke structured-output validation in confusing ways.

    Raises ``RuntimeError`` when the schema is missing or invalid. The
    agents/ directory is checked into git; a missing schema means a
    deployment-level corruption and is worth failing fast on.
    """
    agent_dir = os.path.join(AGENTS_DIR, role)
    schema_path = os.path.join(agent_dir, "schema.json")
    if not os.path.exists(schema_path):
        raise RuntimeError(
            f"agents/{role}/schema.json missing — single-source agent "
            f"schemas require an on-disk schema for each structured role."
        )
    try:
        with open(schema_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"agents/{role}/schema.json invalid: {exc}"
        ) from exc


def load_all_agents(force_reload=False):
    """Load all agent configs from the agents directory."""
    if not os.path.isdir(AGENTS_DIR):
        print(f"[agent loader] agents directory not found: {AGENTS_DIR}")
        return {}
    agents = {}
    for entry in os.listdir(AGENTS_DIR):
        agent_dir = os.path.join(AGENTS_DIR, entry)
        config_path = os.path.join(agent_dir, "agent.yaml")
        if os.path.isdir(agent_dir) and os.path.exists(config_path):
            try:
                agents[entry] = load_agent(entry, force_reload=force_reload)
            except Exception as e:
                print(f"[agent loader] failed to load {entry}: {e}")
    return agents


def reload_all():
    """Force-reload all agents (e.g. after editing configs)."""
    _cache.clear()
    return load_all_agents(force_reload=True)


def list_roles():
    """List available agent roles."""
    if not os.path.isdir(AGENTS_DIR):
        return []
    roles = []
    for entry in sorted(os.listdir(AGENTS_DIR)):
        agent_dir = os.path.join(AGENTS_DIR, entry)
        if os.path.isdir(agent_dir) and os.path.exists(os.path.join(agent_dir, "agent.yaml")):
            roles.append(entry)
    return roles
