"""Application configuration.

Loads ``config.json`` once at import time, validates it against the
``OrchestratorSettings`` Pydantic model (fail-fast on missing required
fields or type errors), and exposes both the raw ``CONFIG`` dict and
the most-used derived constants.  Secrets should never live in
config.json — use ``.env`` (loaded via python-dotenv).

Falls back to ``config.example.json`` when ``config.json`` is missing,
so a fresh checkout (CI runner, new contributor) can boot without a
manual copy step. The example file ships with placeholder targets and
no secrets, so this is safe by construction.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from .config_schema import OrchestratorSettings
from .paths import CONFIG_PATH

_config_path = Path(CONFIG_PATH)
if not _config_path.exists():
    _example_path = _config_path.with_name("config.example.json")
    if _example_path.exists():
        _config_path = _example_path

with open(_config_path) as _f:
    _raw: dict[str, object] = json.load(_f)

try:
    _settings = OrchestratorSettings.model_validate(_raw)
except ValidationError as _exc:
    raise SystemExit(
        f"[core.config] Invalid configuration in {_config_path}:\n{_exc}"
    ) from _exc

# Keep CONFIG as a plain dict for back-compat (prefect_io/__init__.py and
# any other code that does CONFIG.get(...)).
CONFIG: dict[str, object] = _raw

# ---------------------------------------------------------------------------
# Derived module-level constants — read from the validated _settings object
# so all values are type-correct by the time we get here.
# ---------------------------------------------------------------------------

# Ollama endpoints
OLLAMA_MAIN_URL = _settings.ollama.main_url
OLLAMA_JUDGE_URL = _settings.ollama.judge_url
# planner_url defaults to judge_url (reasoning models usually live on the larger box)
OLLAMA_PLANNER_URL = _settings.ollama.planner_url or OLLAMA_JUDGE_URL
OLLAMA_MAIN = OLLAMA_MAIN_URL + "/api/generate"
OLLAMA_JUDGE = OLLAMA_JUDGE_URL + "/api/generate"
OLLAMA_MAIN_CHAT = OLLAMA_MAIN_URL + "/api/chat"
OLLAMA_JUDGE_CHAT = OLLAMA_JUDGE_URL + "/api/chat"
OLLAMA_PLANNER_CHAT = OLLAMA_PLANNER_URL + "/api/chat"
OLLAMA_PLANNER = OLLAMA_PLANNER_URL + "/api/generate"
OLLAMA_EMBED = OLLAMA_MAIN_URL + "/api/embeddings"

# Hindsight memory server (Layer 4)
HINDSIGHT_URL = _settings.hindsight.url
HINDSIGHT_BANK = _settings.hindsight.bank_id
HINDSIGHT_ENABLED = _settings.hindsight.enabled
HINDSIGHT_TIMEOUT = _settings.hindsight.timeout

# Notifications
NOTIFY_CONFIG = CONFIG.get("notifications", {})
NOTIFY_ENABLED = _settings.notifications.enabled
NOTIFY_SERVICE = _settings.notifications.service
NTFY_URL = _settings.notifications.ntfy_url
NTFY_TOPIC = _settings.notifications.ntfy_topic
NTFY_PRIORITY = _settings.notifications.ntfy_priority
GOTIFY_URL = _settings.notifications.gotify_url
# secrets first from env, then config.json (kept for migration grace)
GOTIFY_TOKEN = os.getenv("GOTIFY_TOKEN", "") or _settings.notifications.gotify_token
GOTIFY_PRIORITY = _settings.notifications.gotify_priority
NOTIFY_ON_SUCCESS = _settings.notifications.on_success
NOTIFY_ON_FAILURE = _settings.notifications.on_failure
NTFY_URLS = _settings.notifications.ntfy_urls or [NTFY_URL]
GOTIFY_URLS = _settings.notifications.gotify_urls or ([GOTIFY_URL] if GOTIFY_URL else [])
ORCHESTRATOR_URL = _settings.notifications.orchestrator_url
NOTIFY_STRATEGY = _settings.notifications.strategy

# Vault (Layer 5 — Obsidian / NoteDiscovery)
VAULT_CONFIG = CONFIG.get("vault", {})
VAULT_ENABLED = _settings.vault.enabled
VAULT_LOCAL_DIR = _settings.vault.local_dir
VAULT_REMOTE_HOST = _settings.vault.remote_host
VAULT_REMOTE_USER = _settings.vault.remote_user
VAULT_REMOTE_KEY = _settings.vault.remote_key
VAULT_REMOTE_DIR = _settings.vault.remote_dir
VAULT_SYNC_ENABLED = _settings.vault.sync_enabled
VAULT_NAS_ENABLED = _settings.vault.nas_enabled
VAULT_NAS_PATH = _settings.vault.nas_path

# SSH targets (keyed by name); preserve the original dict shape that consumers expect
SSH_TARGETS = {t.name: t.model_dump() for t in _settings.ssh_targets}
SSH_TIMEOUT = _settings.ssh.timeout

# Persistent deploy base
DEPLOY_BASE = _settings.deploy.base_path

# Autonomy
TARGET_SCORE = _settings.autonomy.target_score
MAX_ITERATIONS = _settings.autonomy.max_iterations
MAX_TROUBLESHOOT_ATTEMPTS = _settings.autonomy.max_troubleshoot_attempts

# Judge fallback
JUDGE_FALLBACK_MODEL = _settings.ollama.judge_fallback_model

# Memory tunables (not in config.json — fixed constants)
SIMILARITY_THRESHOLD = 0.93
REUSE_SCORE_THRESHOLD = 9
MAX_PROMPT_INDEX_ENTRIES = 1000
MAX_EMBED_CACHE_ENTRIES = 2000

# Configurable timeouts (seconds) with sane minimums
TIMEOUT_EMBEDDING = max(30, _settings.timeouts.embedding)
TIMEOUT_LLM_GENERATE = max(60, _settings.timeouts.llm_generate)
TIMEOUT_LLM_STRUCTURED = max(60, _settings.timeouts.llm_structured)
TIMEOUT_HINDSIGHT_RETAIN = max(30, _settings.timeouts.hindsight_retain)
TIMEOUT_HINDSIGHT_RECALL = max(10, _settings.timeouts.hindsight_recall)
TIMEOUT_HINDSIGHT_REFLECT = max(30, _settings.timeouts.hindsight_reflect)
TIMEOUT_VAULT_SYNC = max(30, _settings.timeouts.vault_sync)
TIMEOUT_VAULT_NAS_SYNC = max(15, _settings.timeouts.vault_nas_sync)

# Dream auto-trigger
DREAM_AUTO_INTERVAL = _settings.dream.auto_interval

# Phase 2.1 Postgres durable store. POSTGRES_DSN in .env wins over the
# config.json value so secrets stay out of the config file (mirrors the
# GOTIFY_TOKEN pattern above).
POSTGRES_ENABLED = _settings.postgres.enabled
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "") or _settings.postgres.dsn
POSTGRES_POOL_SIZE = _settings.postgres.pool_size
POSTGRES_POOL_MAX_OVERFLOW = _settings.postgres.pool_max_overflow
POSTGRES_STATEMENT_TIMEOUT_MS = _settings.postgres.statement_timeout_ms
POSTGRES_RECONCILE_ON_STARTUP = _settings.postgres.reconcile_on_startup

# Phase 2.2 Redis ephemeral state. REDIS_URL in .env wins over the
# config.json value (mirrors POSTGRES_DSN pattern).
REDIS_ENABLED = _settings.redis.enabled
REDIS_URL = os.getenv("REDIS_URL", "") or _settings.redis.url
REDIS_SOCKET_CONNECT_TIMEOUT = _settings.redis.socket_connect_timeout
REDIS_SOCKET_TIMEOUT = _settings.redis.socket_timeout
REDIS_RUN_STATUS_TTL = _settings.redis.run_status_ttl
REDIS_URL_CACHE_TTL = _settings.redis.url_cache_ttl
REDIS_EMBED_CACHE_TTL = _settings.redis.embed_cache_ttl

# Phase 2.3 OpenTelemetry tracing. OTEL_ENDPOINT env var wins over the
# config.json value (mirrors POSTGRES_DSN / REDIS_URL pattern).
OTEL_ENABLED = _settings.otel.enabled
OTEL_ENDPOINT = os.getenv("OTEL_ENDPOINT", "") or _settings.otel.endpoint
OTEL_SERVICE_NAME = _settings.otel.service_name
OTEL_SAMPLE_RATIO = _settings.otel.sample_ratio

# Phase 2.4 budget tracking. Rates and thresholds live in config.json
# (no secrets). Materialise the rate map as a plain dict so callers
# don't carry a Pydantic dependency through their type signatures.
BUDGET_ENABLED = _settings.budget.enabled
BUDGET_RATES = {
    name: rate.model_dump()
    for name, rate in _settings.budget.rates_per_million_tokens.items()
}
BUDGET_THRESHOLDS_PCT = list(_settings.budget.thresholds_pct)

# Phase 2.5 SkyPilot cloud-burst. All ships dormant — operators set
# provider creds (e.g. RUNPOD_API_KEY) in .env and flip
# ``sky.enabled=true`` to activate.
SKY_ENABLED = _settings.sky.enabled
SKY_DEFAULT_CLOUD = _settings.sky.default_cloud
SKY_DEFAULT_ACCELERATOR = _settings.sky.default_accelerator
SKY_YAML_DIR = _settings.sky.yaml_dir
SKY_IDLE_TIMEOUT_MINUTES = _settings.sky.idle_timeout_minutes
SKY_MAX_BURST_COST_USD = _settings.sky.max_burst_cost_usd

# Phase 3.2 SmartPause. ``smartpause.enabled`` defaults True; the
# threshold check is inert until ``CampaignTemplate.hitl_mode`` (Phase
# 3.1) ships and a campaign opts out of ``full_auto``.
SMARTPAUSE_ENABLED = _settings.smartpause.enabled
SMARTPAUSE_THRESHOLD = _settings.smartpause.confidence_threshold
SMARTPAUSE_PAUSE_TIMEOUT = int(_settings.smartpause.pause_timeout_seconds)
SMARTPAUSE_POLL_INTERVAL = float(_settings.smartpause.poll_interval_seconds)
