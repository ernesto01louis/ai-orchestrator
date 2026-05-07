#!/usr/bin/env bash
# install_postgres.sh — bootstrap a fresh Debian 12 LXC as the
# Phase 2.1 Postgres durable store for the AI Orchestrator.
#
# Run inside the new LXC after `pct enter <id>`:
#     POSTGRES_ORCHESTRATOR_PASSWORD='<generated-password>' \
#         bash scripts/install_postgres.sh
#
# Idempotent: re-running upgrades the postgres package and re-applies the
# config snippets, but leaves the database, role, and existing data alone.
#
# What this script does:
#   1. Installs postgresql-16 from the official postgresql.org apt repo.
#   2. Creates role `orchestrator` with the password from
#      $POSTGRES_ORCHESTRATOR_PASSWORD (mandatory).
#   3. Creates database `orchestrator` owned by that role.
#   4. Sets listen_addresses='*' so the orchestrator LXC can connect.
#   5. Adds a pg_hba.conf entry allowing scram-sha-256 auth for
#      `orchestrator` from 192.168.2.0/24 (LAN) and 100.64.0.0/10
#      (Tailscale CGNAT range, in case Tailscale gets installed later).
#   6. Adds a daily pg_dump cron writing to /var/backups/postgres/.
#      RUNBOOK.md "Postgres durable store" documents redirecting
#      backups to NFS/NAS for independence from the LXC's own
#      filesystem.
set -euo pipefail

if [[ -z "${POSTGRES_ORCHESTRATOR_PASSWORD:-}" ]]; then
    echo "ERROR: POSTGRES_ORCHESTRATOR_PASSWORD env var must be set." >&2
    echo "Generate one with:  openssl rand -base64 24" >&2
    exit 1
fi

PG_MAJOR="${PG_MAJOR:-16}"

# 1. System deps + official postgresql.org apt repo (postgresql-16 is not
#    in Debian 12 main; Debian 12 ships postgresql-15).
apt-get update
apt-get install -y curl ca-certificates gnupg lsb-release cron

install -d /usr/share/postgresql-common/pgdg
curl -fsS https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list

apt-get update
apt-get install -y "postgresql-${PG_MAJOR}" "postgresql-contrib-${PG_MAJOR}"

PG_CONF_DIR="/etc/postgresql/${PG_MAJOR}/main"
PG_CONF="${PG_CONF_DIR}/postgresql.conf"
PG_HBA="${PG_CONF_DIR}/pg_hba.conf"

# 2. listen_addresses='*' (idempotent — replace the existing line)
sed -i "s/^#\?listen_addresses *= *.*/listen_addresses = '*'/" "$PG_CONF"

# 3. pg_hba.conf: allow scram-sha-256 from LAN + Tailscale CGNAT.
#    Marker comment lets us re-run without duplicating entries.
HBA_MARKER="# orchestrator-app — added by install_postgres.sh"
if ! grep -qF "$HBA_MARKER" "$PG_HBA"; then
    cat >> "$PG_HBA" <<EOF

${HBA_MARKER}
host    orchestrator    orchestrator    192.168.2.0/24       scram-sha-256
host    orchestrator    orchestrator    100.64.0.0/10        scram-sha-256
EOF
fi

systemctl enable --now "postgresql@${PG_MAJOR}-main.service"
systemctl reload "postgresql@${PG_MAJOR}-main.service"

# 4. Role + database (idempotent — CREATE only if absent)
sudo -u postgres psql -v ON_ERROR_STOP=1 -tA <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orchestrator') THEN
        CREATE ROLE orchestrator LOGIN PASSWORD '${POSTGRES_ORCHESTRATOR_PASSWORD}';
    ELSE
        ALTER ROLE orchestrator WITH PASSWORD '${POSTGRES_ORCHESTRATOR_PASSWORD}';
    END IF;
END
\$\$;
SQL

# CREATE DATABASE cannot run inside a DO block; check separately.
DB_EXISTS=$(sudo -u postgres psql -tA -c \
    "SELECT 1 FROM pg_database WHERE datname='orchestrator'")
if [[ -z "$DB_EXISTS" ]]; then
    sudo -u postgres createdb -O orchestrator orchestrator
fi

# 5. Daily pg_dump cron. Default path /var/backups/postgres/ — see
#    RUNBOOK for redirecting to NFS/NAS for backup independence.
install -d -m 0750 -o postgres -g postgres /var/backups/postgres
cat > /etc/cron.daily/orchestrator-pgdump <<'EOF'
#!/bin/sh
# Daily pg_dump for the AI Orchestrator durable store. Installed by
# scripts/install_postgres.sh. Edit BACKUP_DIR to redirect to NFS/NAS.
set -eu
BACKUP_DIR="${BACKUP_DIR:-/var/backups/postgres}"
TS="$(date -u +%Y%m%d-%H%M%S)"
sudo -u postgres pg_dump --format=custom --compress=9 \
    --file="${BACKUP_DIR}/orchestrator-${TS}.dump" orchestrator
# Retain 30 days of dumps.
find "${BACKUP_DIR}" -name 'orchestrator-*.dump' -mtime +30 -delete
EOF
chmod 0755 /etc/cron.daily/orchestrator-pgdump

# 6. Health check
if ! sudo -u postgres psql -tA -c 'SELECT 1' orchestrator >/dev/null; then
    echo "ERROR: orchestrator database is not reachable from local socket." >&2
    exit 1
fi

cat <<EOF

Done. Postgres ${PG_MAJOR} is up. From the orchestrator LXC, set the DSN:

    POSTGRES_DSN=postgresql://orchestrator:<password>@<this-lxc-ip>:5432/orchestrator

(use the same password you passed to this script).

Then on the orchestrator LXC, run \`alembic upgrade head\` to apply
the schema before flipping \`postgres.enabled=true\` in config.json.
EOF
