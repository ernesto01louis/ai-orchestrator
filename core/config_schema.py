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


class SkyConfig(_FlexModel):
    """Phase 2.5 SkyPilot cloud-burst configuration.

    All defaults make ``sky.enabled=False`` the dormant state — every
    entry point in ``core.sky`` exits before contacting any provider
    until the operator both flips ``enabled`` and sets up provider
    credentials per the SkyPilot docs (e.g. ``runpod login`` or
    ``RUNPOD_API_KEY`` in ``.env``).

    ``yaml_dir`` points at the orchestrator-managed pool of YAML specs
    (``sky/llm-burst.yaml`` etc.). ``idle_timeout_minutes`` is the
    safety net for the 2.5.4 idle-stop daemon — clusters with no
    activity for this long get auto-stopped to bound cost. ``max_burst_
    cost_usd`` lets operators cap individual bursts (independent of
    the per-campaign Phase 2.4 budget).
    """

    enabled: bool = False
    # Default cloud — accepted by ``sky launch -c``. Common choices:
    # ``runpod``, ``vast``, ``aws``, ``gcp``. Per-spec overrides win.
    default_cloud: str = "runpod"
    # Default GPU accelerator string (matches sky.Resources(accelerators=...)).
    default_accelerator: str = "A10:1"
    # Working dir on the orchestrator that the burst route looks in for
    # named YAML specs.
    yaml_dir: str = "sky"
    # Minutes a burst can sit idle before the failsafe stops it. Set
    # to 0 to disable idle-stop (operators who want manual control).
    idle_timeout_minutes: int = 30
    # Per-burst USD ceiling. Independent of the campaign budget — the
    # burst is rejected at launch time if its requested resources
    # exceed this estimate.
    max_burst_cost_usd: float = 5.0


class BudgetRate(_FlexModel):
    """Per-model cost rate. Both numbers are USD per 1M tokens.

    Local Ollama models default to ``0.0`` (electricity is below the
    measurement threshold). Operators flip to non-zero rates when their
    consumer projects route through a paid provider.
    """

    prompt: float = 0.0
    completion: float = 0.0


class BudgetConfig(_FlexModel):
    """Phase 2.4 budget tracking.

    Rates are keyed by model name. The ``default`` entry is the
    fallback for any model not in the map. Phase 2.5 SkyPilot will add
    a separate ``cloud_gpu_per_hour`` block.
    """

    enabled: bool = False
    rates_per_million_tokens: dict[str, BudgetRate] = {
        "default": BudgetRate(prompt=0.0, completion=0.0),
    }
    # Threshold percentages that trigger a notification (sorted ascending).
    # 100 ALSO triggers an auto-pause on the campaign. Operators can drop
    # ``100`` here to disable auto-pause without losing the warning.
    thresholds_pct: list[int] = [50, 80, 100]


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

class NoteDiscoveryConfig(_FlexModel):
    """Phase 3.3 NoteDiscovery-grounded planner.

    The planner queries the operator's NoteDiscovery vault before
    proposing a campaign, seeds ``params`` from cited content, and
    extends the Phase 1.2 evidence bundle with a ``references``
    array.

    Defaults dormant (``enabled=False``); flip in ``config.json``
    once the LXC at ``base_url`` is reachable. The orchestrator
    fail-tolerates a down NoteDiscovery — the planner falls back to
    its existing memory stack.
    """

    enabled: bool = False
    base_url: str = "http://192.168.2.203:8010"
    top_k: int = 8
    timeout_seconds: int = 30


class HITLConfig(_FlexModel):
    """Phase 3.1 HITL (human-in-the-loop) intervention modes.

    A campaign declares its desired interventionism via
    ``CampaignTemplate.hitl_mode``; this block controls system-wide
    defaults and the timeout for each pause.

    ``default_mode`` is the fall-back when a campaign omits
    ``hitl_mode`` (or for one-shot orchestrations that aren't part of
    a campaign at all). Always ``"full_auto"`` to keep existing
    behaviour unchanged.

    ``intervention_timeout_seconds`` bounds how long the orchestration
    loop waits for an operator's POST to ``/runs/{id}/intervene``
    before timing out and continuing. Default 1h is opinionated;
    raise for unattended deployments.
    """

    default_mode: str = "full_auto"
    intervention_timeout_seconds: int = 3600
    poll_interval_seconds: float = 2.0


class SmartPauseConfig(_FlexModel):
    """Phase 3.2 SmartPause.

    The planner returns a self-reported ``confidence: float`` in
    ``[0, 1]`` (added to ``agents/planner/schema.json`` in Phase 3.2).
    When ``confidence < threshold`` AND the campaign's ``hitl_mode``
    is anything but ``full_auto``, the orchestrator auto-pauses the
    run and notifies the operator.

    Until Phase 3.1 lands ``hitl_mode``, the threshold check is inert
    in practice — every campaign defaults to ``full_auto`` so nothing
    pauses. The infrastructure (config, schema field, accrual) ships
    in 3.2 so 3.1's HITL modes can rely on it without further plumbing.
    """

    enabled: bool = True
    confidence_threshold: float = 0.7
    # How long the orchestration loop waits for a /runs/{id}/resume POST
    # before timing out and continuing the run. 1h is opinionated; raise
    # for unattended deployments, lower for tight feedback loops.
    pause_timeout_seconds: int = 3600
    # How often the polling loop checks for the resume flag. Two seconds
    # is a fine compromise between responsiveness and CPU.
    poll_interval_seconds: float = 2.0


class ChunkingConfig(_FlexModel):
    """Repo-screening spike (2026-05-11): chonkie-backed text chunking.

    Currently dormant. The orchestrator embeds whole texts today
    (``memory_pkg.generate_embedding`` / ``references_pkg`` PDF→md);
    chunking exists as an available primitive in ``core/chunking.py``
    for future use (RAG, finer-grained embedding-cache stability under
    one-line edits).

    Flipping ``enabled=true`` does not by itself change behaviour —
    callsites must opt in explicitly via ``core.chunking.chunk_text``.
    """

    enabled: bool = False
    # Chonkie chunker variant. Only ``recursive`` is wired today; the
    # field is kept as a Literal so adding more variants is a controlled
    # extension rather than a wildcard string.
    chunker: str = "recursive"
    # Target chunk size in characters. 1024 is conservative for
    # ``nomic-embed-text`` (8192-token context); raise once measurement
    # shows we're cache-thrashing on small docs.
    chunk_size: int = 1024
    # Character overlap between adjacent chunks — keeps cross-boundary
    # semantics intact for nearest-neighbour search.
    chunk_overlap: int = 128


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
    budget: BudgetConfig = BudgetConfig()
    sky: SkyConfig = SkyConfig()
    smartpause: SmartPauseConfig = SmartPauseConfig()
    hitl: HITLConfig = HITLConfig()
    note_discovery: NoteDiscoveryConfig = NoteDiscoveryConfig()
    chunking: ChunkingConfig = ChunkingConfig()
