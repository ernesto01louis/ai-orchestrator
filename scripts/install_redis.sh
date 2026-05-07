#!/usr/bin/env bash
# install_redis.sh — bootstrap a fresh Debian 12 LXC as the
# Phase 2.2 Redis ephemeral state store for the AI Orchestrator.
#
# Run inside the new LXC after `pct enter <id>`:
#     bash scripts/install_redis.sh
#
# Optional: pre-set REDIS_ORCHESTRATOR_PASSWORD to use a specific
# password; if unset the script generates a URL-safe alphanumeric one
# and prints it at the end. URL-safe means no `/+=@:?#%&`, so the
# password can be dropped straight into REDIS_URL without encoding.
#
# Idempotent: re-running upgrades the package and re-applies the
# config snippets, but leaves the AOF/dump files alone.
#
# What this script does:
#   1. Installs sudo + redis-server (Debian 12 ships Redis 7.0.x —
#      sufficient for all Phase 2.2 features: pub/sub, BLPOP, hashes,
#      EXPIRE, ACL is unused since we use requirepass).
#   2. Generates REDIS_ORCHESTRATOR_PASSWORD if not supplied.
#   3. Configures /etc/redis/redis.conf:
#        bind 0.0.0.0 ::               (LAN/tailnet reachable)
#        protected-mode no             (auth handled by requirepass)
#        requirepass <password>        (clients must AUTH first)
#        appendonly yes                (AOF persistence survives restart)
#        appendfsync everysec          (≤1s data loss on crash, fast)
#        maxmemory-policy allkeys-lru  (cache-friendly eviction)
#   4. Restarts redis-server so config takes effect.
#   5. Health-checks via `redis-cli ping` with auth.
#
# No backup cron is installed — by design Redis holds ephemeral state
# only (live RUN_STATUS, pub/sub, caches). Completed runs are canonical
# in memory/run_index.json and mirrored to Postgres (Phase 2.1). If you
# want backups anyway, see RUNBOOK § "Redis ephemeral store" → "Backup
# (optional)".
set -euo pipefail

# 1. System deps. Debian 12 base ships redis 7.0.x (sufficient).
apt-get update
apt-get install -y sudo redis-server openssl

REDIS_CONF="/etc/redis/redis.conf"

# 2. Generate the requirepass password if the caller didn't pre-set
#    one. Strip URL-special bytes from openssl's base64 alphabet so the
#    password is safe to interpolate into REDIS_URL.
PASSWORD_GENERATED=0
if [[ -z "${REDIS_ORCHESTRATOR_PASSWORD:-}" ]]; then
    REDIS_ORCHESTRATOR_PASSWORD="$(openssl rand -base64 36 | tr -d '/+=@:?#%&' | head -c 32)"
    PASSWORD_GENERATED=1
fi

# 3. Configure redis.conf — idempotent edits.
# bind: Debian default is "127.0.0.1 -::1"; widen to LAN.
sed -i "s|^bind .*|bind 0.0.0.0 ::|" "$REDIS_CONF"

# protected-mode: turn off; requirepass takes over.
sed -i "s|^protected-mode .*|protected-mode no|" "$REDIS_CONF"

# requirepass: replace existing or append.
if grep -q '^requirepass ' "$REDIS_CONF"; then
    sed -i "s|^requirepass .*|requirepass ${REDIS_ORCHESTRATOR_PASSWORD}|" "$REDIS_CONF"
else
    echo "requirepass ${REDIS_ORCHESTRATOR_PASSWORD}" >> "$REDIS_CONF"
fi

# AOF persistence: live RUN_STATUS survives a Redis restart. Not
# canonical — completed runs live in memory/run_index.json + Postgres.
sed -i "s|^appendonly .*|appendonly yes|" "$REDIS_CONF"
sed -i "s|^appendfsync .*|appendfsync everysec|" "$REDIS_CONF"

# Cache-friendly eviction once Phase 2.2.4 caches fill memory.
if grep -q '^maxmemory-policy ' "$REDIS_CONF"; then
    sed -i "s|^maxmemory-policy .*|maxmemory-policy allkeys-lru|" "$REDIS_CONF"
else
    echo "maxmemory-policy allkeys-lru" >> "$REDIS_CONF"
fi

# 4. Restart so bind + requirepass take effect (reload is not enough
#    for bind changes).
systemctl enable --now redis-server
systemctl restart redis-server

# 5. Health check — auth required now.
if ! redis-cli -a "$REDIS_ORCHESTRATOR_PASSWORD" --no-auth-warning ping | grep -q PONG; then
    echo "ERROR: redis-server did not respond to PING with auth." >&2
    exit 1
fi

cat <<EOF

Done. Redis 7 is up. From the orchestrator LXC, set the URL in .env:

    REDIS_URL=redis://:${REDIS_ORCHESTRATOR_PASSWORD}@<this-lxc-ip>:6379/0

Then flip \`redis.enabled=true\` in config.json and restart the
orchestrator service. The redis_real test marker can be exercised with:

    REDIS_URL='...' python -m pytest -m redis_real -q

EOF

if [[ "$PASSWORD_GENERATED" -eq 1 ]]; then
    cat <<EOF
NOTE: this script generated the requirepass password (no
REDIS_ORCHESTRATOR_PASSWORD env var was supplied). Save it BEFORE
this terminal closes — it is not stored anywhere on disk other than
/etc/redis/redis.conf:
    ${REDIS_ORCHESTRATOR_PASSWORD}
EOF
fi
