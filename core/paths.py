"""Filesystem paths used by the orchestrator.

Single source of truth — modules elsewhere (dream, gates, app, ...) should
import from here rather than hardcoding /opt/ai-orchestrator paths.

Mirror the historical types: PROJECTS_DIR/LOG_DIR/MEMORY_DIR/SANDBOX_DIR/
SANDBOX_VENV are strings (used in f-strings and shell commands); the
per-file constants below are Path objects.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path("/opt/ai-orchestrator")

CONFIG_PATH = str(REPO_ROOT / "config.json")

PROJECTS_DIR = "/opt/ai-orchestrator/projects"
LOG_DIR = "/opt/ai-orchestrator/logs"
MEMORY_DIR = "/opt/ai-orchestrator/memory"

# Vault default location (overridable via config "vault.local_dir")
VAULT_DIR_DEFAULT = "/opt/ai-orchestrator/vault"

# Sandbox (disposable, for in-process testing)
SANDBOX_DIR = "/tmp/ai_sandbox"
SANDBOX_VENV = "/tmp/ai_env"

# Memory data files (JSON / Markdown) — Path objects to match historical usage
RUN_INDEX_FILE = Path(MEMORY_DIR) / "run_index.json"
PROMPT_INDEX = Path(MEMORY_DIR) / "prompt_index.json"
EMBED_CACHE = Path(MEMORY_DIR) / "embedding_cache.json"
NEGATIVE_MEMORY = Path(MEMORY_DIR) / "negative_memory.json"
MODEL_STATS = Path(MEMORY_DIR) / "model_stats.json"
SESSION_LOG = Path(MEMORY_DIR) / "session_log.json"
IDENTITY_FILE = Path(MEMORY_DIR) / "identity.md"
PRIMER_FILE = Path(MEMORY_DIR) / "primer.md"
GOALS_FILE = Path(MEMORY_DIR) / "goals.md"
TARGET_IDENTITY_DIR = Path(MEMORY_DIR) / "targets"

# References (PDFs, docs, code samples ingested into RAG)
REFERENCE_DIR = Path("/opt/ai-orchestrator/references")

# Gates (safety) — historically lived at repo root
GATES_FILE = "/opt/ai-orchestrator/gates.json"
GATES_LOG = "/opt/ai-orchestrator/memory/gates_log.json"
LESSONS_DIR = "/opt/ai-orchestrator/memory/lessons"

# Dream (memory consolidation log)
DREAM_LOG = Path(MEMORY_DIR) / "dream_log.json"

# Tool registry
TOOL_REGISTRY_PATH = "/opt/ai-orchestrator/tool_registry.json"

# Campaigns (Phase 1.1) — durable state + YAML templates
CAMPAIGNS_FILE = Path(MEMORY_DIR) / "campaigns.json"
CAMPAIGN_TEMPLATES_DIR = REPO_ROOT / "campaigns"

# Ensure runtime directories exist (idempotent)
Path(PROJECTS_DIR).mkdir(parents=True, exist_ok=True)
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
TARGET_IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
CAMPAIGN_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
