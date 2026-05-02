"""Application configuration.

Loads ``config.json`` once at import time and exposes both the raw
``CONFIG`` dict and the most-used derived constants. Secrets should
never live in config.json — use ``.env`` (loaded via python-dotenv).

Falls back to ``config.example.json`` when ``config.json`` is missing,
so a fresh checkout (CI runner, new contributor) can boot without a
manual copy step. The example file ships with placeholder targets and
no secrets, so this is safe by construction.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import CONFIG_PATH

_config_path = Path(CONFIG_PATH)
if not _config_path.exists():
    _example_path = _config_path.with_name("config.example.json")
    if _example_path.exists():
        _config_path = _example_path

with open(_config_path) as _f:
    CONFIG: dict = json.load(_f)

# Ollama endpoints
OLLAMA_MAIN_URL = CONFIG["ollama"]["main_url"]
OLLAMA_JUDGE_URL = CONFIG["ollama"]["judge_url"]
# planner_url defaults to judge_url (reasoning models usually live on the larger box)
OLLAMA_PLANNER_URL = CONFIG["ollama"].get("planner_url", OLLAMA_JUDGE_URL)
OLLAMA_MAIN = OLLAMA_MAIN_URL + "/api/generate"
OLLAMA_JUDGE = OLLAMA_JUDGE_URL + "/api/generate"
OLLAMA_MAIN_CHAT = OLLAMA_MAIN_URL + "/api/chat"
OLLAMA_JUDGE_CHAT = OLLAMA_JUDGE_URL + "/api/chat"
OLLAMA_PLANNER_CHAT = OLLAMA_PLANNER_URL + "/api/chat"
OLLAMA_PLANNER = OLLAMA_PLANNER_URL + "/api/generate"
OLLAMA_EMBED = OLLAMA_MAIN_URL + "/api/embeddings"

# Hindsight memory server (Layer 4)
HINDSIGHT_URL = CONFIG.get("hindsight", {}).get("url", "http://192.168.2.203:8888")
HINDSIGHT_BANK = CONFIG.get("hindsight", {}).get("bank_id", "Orchestrator")
HINDSIGHT_ENABLED = CONFIG.get("hindsight", {}).get("enabled", True)
HINDSIGHT_TIMEOUT = CONFIG.get("hindsight", {}).get("timeout", 120)

# Notifications
NOTIFY_CONFIG = CONFIG.get("notifications", {})
NOTIFY_ENABLED = NOTIFY_CONFIG.get("enabled", False)
NOTIFY_SERVICE = NOTIFY_CONFIG.get("service", "ntfy")
NTFY_URL = NOTIFY_CONFIG.get("ntfy_url", "https://ntfy.sh")
NTFY_TOPIC = NOTIFY_CONFIG.get("ntfy_topic", "ai-orchestrator")
NTFY_PRIORITY = NOTIFY_CONFIG.get("ntfy_priority", "default")
GOTIFY_URL = NOTIFY_CONFIG.get("gotify_url", "")
# secrets first from env, then config.json (kept for migration grace)
GOTIFY_TOKEN = os.getenv("GOTIFY_TOKEN", "") or NOTIFY_CONFIG.get("gotify_token", "")
GOTIFY_PRIORITY = NOTIFY_CONFIG.get("gotify_priority", 5)
NOTIFY_ON_SUCCESS = NOTIFY_CONFIG.get("on_success", True)
NOTIFY_ON_FAILURE = NOTIFY_CONFIG.get("on_failure", True)
NTFY_URLS = NOTIFY_CONFIG.get("ntfy_urls", [NTFY_URL])
GOTIFY_URLS = NOTIFY_CONFIG.get("gotify_urls", [GOTIFY_URL] if GOTIFY_URL else [])
ORCHESTRATOR_URL = NOTIFY_CONFIG.get("orchestrator_url", "http://192.168.2.216:8000")
NOTIFY_STRATEGY = NOTIFY_CONFIG.get("strategy", "failover")

# Vault (Layer 5 — Obsidian / NoteDiscovery)
VAULT_CONFIG = CONFIG.get("vault", {})
VAULT_ENABLED = VAULT_CONFIG.get("enabled", False)
VAULT_LOCAL_DIR = VAULT_CONFIG.get("local_dir", "/opt/ai-orchestrator/vault")
VAULT_REMOTE_HOST = VAULT_CONFIG.get("remote_host", "")
VAULT_REMOTE_USER = VAULT_CONFIG.get("remote_user", "root")
VAULT_REMOTE_KEY = VAULT_CONFIG.get("remote_key", "/root/.ssh/id_rsa")
VAULT_REMOTE_DIR = VAULT_CONFIG.get("remote_dir", "/opt/notediscovery/data")
VAULT_SYNC_ENABLED = VAULT_CONFIG.get("sync_enabled", True)
VAULT_NAS_ENABLED = VAULT_CONFIG.get("nas_enabled", False)
VAULT_NAS_PATH = VAULT_CONFIG.get("nas_path", "/mnt/nas-vault/ai-orchestrator-vault")

# SSH targets (keyed by name)
SSH_TARGETS = {t["name"]: t for t in CONFIG["ssh_targets"]}
SSH_TIMEOUT = CONFIG.get("ssh", {}).get("timeout", 120)

# Persistent deploy base
DEPLOY_BASE = CONFIG.get("deploy", {}).get("base_path", "~/ai-projects")

# Autonomy
TARGET_SCORE = CONFIG["autonomy"]["target_score"]
MAX_ITERATIONS = CONFIG["autonomy"]["max_iterations"]
MAX_TROUBLESHOOT_ATTEMPTS = CONFIG["autonomy"].get("max_troubleshoot_attempts", 3)

# Judge fallback
JUDGE_FALLBACK_MODEL = CONFIG["ollama"].get("judge_fallback_model", "")

# Memory tunables
SIMILARITY_THRESHOLD = 0.93
REUSE_SCORE_THRESHOLD = 9
MAX_PROMPT_INDEX_ENTRIES = 1000
MAX_EMBED_CACHE_ENTRIES = 2000

# Configurable timeouts (seconds) with sane minimums
_TIMEOUTS = CONFIG.get("timeouts", {})
TIMEOUT_EMBEDDING = max(30, _TIMEOUTS.get("embedding", 1800))
TIMEOUT_LLM_GENERATE = max(60, _TIMEOUTS.get("llm_generate", 2400))
TIMEOUT_LLM_STRUCTURED = max(60, _TIMEOUTS.get("llm_structured", 2400))
TIMEOUT_HINDSIGHT_RETAIN = max(30, _TIMEOUTS.get("hindsight_retain", 600))
TIMEOUT_HINDSIGHT_RECALL = max(10, _TIMEOUTS.get("hindsight_recall", 120))
TIMEOUT_HINDSIGHT_REFLECT = max(30, _TIMEOUTS.get("hindsight_reflect", 600))
TIMEOUT_VAULT_SYNC = max(30, _TIMEOUTS.get("vault_sync", 180))
TIMEOUT_VAULT_NAS_SYNC = max(15, _TIMEOUTS.get("vault_nas_sync", 120))

# Dream auto-trigger
DREAM_AUTO_INTERVAL = CONFIG.get("dream", {}).get("auto_interval", 10)
