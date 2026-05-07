"""Pydantic v2 schema for config.json.

Validates the orchestrator's configuration file at import time so that
missing required fields and type errors surface with a clear message
rather than as AttributeError deep in a request path.

Design choices:
- ``extra="allow"`` on every model: ``_comment`` / ``_*_note`` keys in the
  JSON are documentation-only and must be tolerated without warnings.
- Required vs. optional mirrors today's ``CONFIG[...]`` vs.
  ``CONFIG.get(..., default)`` pattern in ``core/config.py``.
- No ``HttpUrl`` validators: orchestrator URLs are LAN/Tailscale strings
  that the codebase string-concatenates (``+ "/api/chat"``); ``HttpUrl``
  would return an object that breaks those concatenations.
- ``pydantic-settings`` is already vendored but we use plain
  ``BaseModel`` here because the source is a JSON file we load ourselves,
  not environment variables.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Shared base that allows unknown keys (tolerates _comment / _*_note)
# ---------------------------------------------------------------------------

class _FlexModel(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Nested group models
# ---------------------------------------------------------------------------

class OllamaConfig(_FlexModel):
    # Required (raw CONFIG["ollama"]["main_url"] / ["judge_url"])
    main_url: str
    judge_url: str
    # Optional — defaults mirror config.py
    planner_url: str = ""        # falls back to judge_url in config.py
    judge_fallback_model: str = ""


class HindsightConfig(_FlexModel):
    url: str = "http://192.168.2.203:8888"
    bank_id: str = "Orchestrator"
    enabled: bool = True
    timeout: int = 120


class NotificationsConfig(_FlexModel):
    enabled: bool = False
    service: str = "ntfy"
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = "ai-orchestrator"
    ntfy_priority: str = "default"
    gotify_url: str = ""
    gotify_token: str = ""
    gotify_priority: int = 5
    on_success: bool = True
    on_failure: bool = True
    ntfy_urls: list[str] = []
    gotify_urls: list[str] = []
    orchestrator_url: str = "http://192.168.2.216:8000"
    strategy: str = "failover"


class VaultConfig(_FlexModel):
    enabled: bool = False
    local_dir: str = "/opt/ai-orchestrator/vault"
    remote_host: str = ""
    remote_user: str = "root"
    remote_key: str = "/root/.ssh/id_rsa"
    remote_dir: str = "/opt/notediscovery/data"
    sync_enabled: bool = True
    nas_enabled: bool = False
    nas_path: str = "/mnt/nas-vault/ai-orchestrator-vault"


class GenerationConfig(_FlexModel):
    parallel: bool = True


class AutonomyConfig(_FlexModel):
    # Required (raw CONFIG["autonomy"]["target_score"] / ["max_iterations"])
    target_score: float
    max_iterations: int
    # Optional
    enabled: bool = True
    max_troubleshoot_attempts: int = 3


class SshConfig(_FlexModel):
    timeout: int = 120


class TimeoutsConfig(_FlexModel):
    embedding: int = 1800
    llm_generate: int = 2400
    llm_structured: int = 2400
    hindsight_retain: int = 600
    hindsight_recall: int = 120
    hindsight_reflect: int = 600
    vault_sync: int = 180
    vault_nas_sync: int = 120


class SudoConfig(_FlexModel):
    enabled: bool = False
    allowed_commands: list[str] = []


class DeployConfig(_FlexModel):
    base_path: str = "~/ai-projects"


class DebugConfig(_FlexModel):
    verbose: bool = False


class SshTargetConfig(_FlexModel):
    """One entry in the ``ssh_targets`` list."""
    name: str
    host: str
    username: str
    key_path: str


class CloudImageGenFallback(_FlexModel):
    provider: str = "gemini"
    model: str = "gemini-2.5-flash-image"
    api_key: str = ""


class CloudImageGenConfig(_FlexModel):
    enabled: bool = False
    provider: str = "replicate"
    api_key: str = ""
    default_model: str = "black-forest-labs/flux-dev"
    fallback: CloudImageGenFallback = CloudImageGenFallback()


class PrefectConfig(_FlexModel):
    api_url: str = "http://prefect.tailnet:4200/api"
    execution_mode: str = "in_process"
    work_pool: str = "orchestrator-pool"


class DreamConfig(_FlexModel):
    auto_interval: int = 10


class PostgresConfig(_FlexModel):
    """Phase 2.1 durable system-of-record (Postgres in its own LXC).

    JSON files under ``memory/`` / ``runs/`` / ``campaigns/`` remain
    canonical; Postgres is the queryable mirror. Dual-writes are
    JSON-first, Postgres-second. Defaults keep the feature dormant
    (``enabled=False``) so the LXC can come up after the application
    code has shipped.
    """

    enabled: bool = False
    # DSN may be empty when ``enabled=False``. When enabled, the value
    # in config.json is overridden by the ``POSTGRES_DSN`` env var if
    # set (see core/config.py for the precedence wiring).
    dsn: str = ""
    pool_size: int = 5
    pool_max_overflow: int = 5
    statement_timeout_ms: int = 5000
    reconcile_on_startup: bool = True


class OTelConfig(_FlexModel):
    """Phase 2.3 OpenTelemetry tracing configuration.

    Tracing is dormant by default (``enabled=False``); when activated,
    the orchestrator initialises a global TracerProvider that exports
    spans via OTLP/gRPC to ``endpoint`` and auto-instruments FastAPI
    + the ``requests`` library. ``sample_ratio`` is a head-based ratio
    in ``[0, 1]`` — ``1.0`` records every trace, ``0.1`` ten percent.
    """

    enabled: bool = False
    # OTLP/gRPC endpoint, e.g. "tempo.tailnet:4317" or "192.168.2.187:4317".
    # Env var ``OTEL_ENDPOINT`` overrides config.json (mirrors POSTGRES_DSN
    # / REDIS_URL precedence).
    endpoint: str = ""
    service_name: str = "ai-orchestrator"
    sample_ratio: float = 1.0


class RedisConfig(_FlexModel):
    """Phase 2.2 ephemeral state store (Redis in its own LXC).

    In-process state under ``core/runtime`` (RUN_STATUS, ws clients) and
    process-local caches in ``llm/ollama`` and ``memory_pkg`` remain the
    fast path; Redis is the cross-process coordination + survives-restart
    layer. Defaults keep the feature dormant (``enabled=False``) so the
    LXC can come up after the application code has shipped.
    """

    enabled: bool = False
    # URL may be empty when ``enabled=False``. When enabled, the value
    # in config.json is overridden by the ``REDIS_URL`` env var if set
    # (mirrors POSTGRES_DSN precedence).
    url: str = ""
    socket_connect_timeout: float = 2.0
    socket_timeout: float = 5.0
    # TTLs (seconds) — caches that move to Redis use these defaults
    # unless their callsite passes an explicit ttl.
    run_status_ttl: int = 86400  # 1 day; keeps completed runs queryable while live
    url_cache_ttl: int = 60  # mirrors today's TTL_CACHE in llm/ollama
    embed_cache_ttl: int = 604800  # 7 days; embeddings rarely change


# ---------------------------------------------------------------------------
# Top-level settings model
# ---------------------------------------------------------------------------

class OrchestratorSettings(_FlexModel):
    """Validated view of config.json.

    Required fields (no default) mirror the ``CONFIG["x"]`` accesses that
    would raise ``KeyError`` today.  Optional fields keep their existing
    defaults from ``core/config.py``.
    """

    # Required groups
    ollama: OllamaConfig
    autonomy: AutonomyConfig
    # Required list (may be empty, but the key must exist)
    ssh_targets: list[SshTargetConfig]

    # Optional groups with sane defaults
    hindsight: HindsightConfig = HindsightConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    vault: VaultConfig = VaultConfig()
    generation: GenerationConfig = GenerationConfig()
    ssh: SshConfig = SshConfig()
    timeouts: TimeoutsConfig = TimeoutsConfig()
    sudo: SudoConfig = SudoConfig()
    deploy: DeployConfig = DeployConfig()
    debug: DebugConfig = DebugConfig()
    cloud_image_gen: CloudImageGenConfig = CloudImageGenConfig()
    mcp_servers: dict[str, object] = {}
    prefect: PrefectConfig = PrefectConfig()
    dream: DreamConfig = DreamConfig()
    postgres: PostgresConfig = PostgresConfig()
    redis: RedisConfig = RedisConfig()
    otel: OTelConfig = OTelConfig()
