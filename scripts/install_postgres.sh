#!/usr/bin/env bash
# install_postgres.sh — bootstrap a fresh Debian 12 LXC as the
# Phase 2.1 Postgres durable store for the AI Orchestrator.
#
# Run inside the new LXC after `pct enter <id>`:
#     bash scripts/install_postgres.sh
#
# Optional: pre-set POSTGRES_ORCHESTRATOR_PASSWORD to use a specific
# password; if unset the script generates an alphanumeric one and
# prints it at the end. Auto-generated passwords are URL-safe (no /+=)
# so they can be dropped straight into POSTGRES_DSN without encoding
# AND don't trip ConfigParser %-interpolation in alembic.ini.
#
# Idempotent: re-running upgrades the postgres package and re-applies
# the config snippets, but leaves the database, role, and existing
# data alone.
#
# What this script does:
#   1. Installs sudo + postgresql-16 from the official postgresql.org
#      apt repo + locales-all (so initdb gets a real UTF-8 default).
#   2. Generates POSTGRES_ORCHESTRATOR_PASSWORD if not supplied.
#   3. Creates role `orchestrator` with that password.
#   4. Creates database `orchestrator` from `template0` with explicit
#      UTF-8 / LC_COLLATE=C / LC_CTYPE=C — initdb on a locale-less LXC
#      otherwise falls back to SQL_ASCII, which makes psycopg return
#      bytes for `SELECT version()` and breaks SQLAlchemy 2.0.
#   5. Sets listen_addresses='*' so the orchestrator LXC can connect.
#   6. Adds a pg_hba.conf entry allowing scram-sha-256 auth for
#      `orchestrator` from 192.168.2.0/24 (LAN) and 100.64.0.0/10
#      (Tailscale CGNAT range, in case Tailscale gets installed later).
#   7. Restarts postgres so listen_addresses + pg_hba take effect
#      (a `reload` is NOT enough for listen_addresses).
#   8. Adds a daily pg_dump cron writing to /var/backups/postgres/.
#      RUNBOOK.md "Postgres durable store" → "Backup independence"
#      walks through redirecting that to NFS/NAS for true backup
#      independence from this LXC's filesystem.
set -euo pipefail

PG_MAJOR="${PG_MAJOR:-16}"

# 1. System deps + official postgresql.org apt repo (postgresql-16 is
#    not in Debian 12 main; Debian 12 ships postgresql-15). locales-all
#    silences `perl: Setting locale failed` warnings the postgres tools
#    emit on a locale-less LXC.
apt-get update
apt-get install -y sudo curl ca-certificates gnupg lsb-release cron locales-all

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

# 2. Generate the role password if the caller didn't pre-set one.
#    Strip URL-special bytes from openssl's base64 alphabet so the
#    password is safe to interpolate into both POSTGRES_DSN and
#    alembic.ini's %-aware ConfigParser.
PASSWORD_GENERATED=0
if [[ -z "${POSTGRES_ORCHESTRATOR_PASSWORD:-}" ]]; then
    POSTGRES_ORCHESTRATOR_PASSWORD="$(openssl rand -base64 36 | tr -d '/+=@:?#%&' | head -c 32)"
    PASSWORD_GENERATED=1
fi

# 3. listen_addresses='*' (idempotent — replace the existing line)
sed -i "s/^#\?listen_addresses *= *.*/listen_addresses = '*'/" "$PG_CONF"

# 4. pg_hba.conf: allow scram-sha-256 from LAN + Tailscale CGNAT.
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

# 5. Role + database (idempotent — CREATE only if absent).
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
# Use template0 with explicit UTF-8 / LC_COLLATE=C / LC_CTYPE=C so we
# don't inherit the cluster's SQL_ASCII default on a locale-less LXC.
DB_EXISTS=$(sudo -u postgres psql -tA -c \
    "SELECT 1 FROM pg_database WHERE datname='orchestrator'")
if [[ -z "$DB_EXISTS" ]]; then
    sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres -c \
        "CREATE DATABASE orchestrator OWNER orchestrator TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
fi

# 6. Restart (NOT reload) so listen_addresses + pg_hba take effect.
systemctl restart "postgresql@${PG_MAJOR}-main.service"

# 7. Daily pg_dump cron. Default path /var/backups/postgres/ — see
#    RUNBOOK § "Backup independence" for redirecting to NFS/NAS.
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

# 8. Health check
if ! sudo -u postgres psql -tA -c 'SELECT 1' orchestrator >/dev/null; then
    echo "ERROR: orchestrator database is not reachable from local socket." >&2
    exit 1
fi

cat <<EOF

Done. Postgres ${PG_MAJOR} is up. From the orchestrator LXC, set the DSN:

    POSTGRES_DSN=postgresql://orchestrator:${POSTGRES_ORCHESTRATOR_PASSWORD}@<this-lxc-ip>:5432/orchestrator

Then on the orchestrator LXC, run \`alembic upgrade head\` to apply
the schema before flipping \`postgres.enabled=true\` in config.json.
EOF

if [[ "$PASSWORD_GENERATED" -eq 1 ]]; then
    cat <<EOF

NOTE: this script generated the role password (no
POSTGRES_ORCHESTRATOR_PASSWORD was set in the environment). Save it now —
it's not stored anywhere else.

Generated password: ${POSTGRES_ORCHESTRATOR_PASSWORD}
EOF
fi

cat <<'EOF'

For backup independence (RUNBOOK § "Backup independence"): mount NFS
or SMB from your NAS at /mnt/nas-pgbackup/ and edit
/etc/cron.daily/orchestrator-pgdump to set BACKUP_DIR=/mnt/nas-pgbackup.
The default path on local disk does NOT survive an LXC-disk failure.
EOF
