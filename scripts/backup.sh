#!/usr/bin/env bash
# Nightly backup of the orchestrator's stateful tree to TrueNAS.
# See RESTORE.md for the full disaster-recovery procedure.
#
# What this captures (the "stateful runtime data" bucket):
#   memory/  vault/  projects/  config.json  .env  gates.json
#   gates_log.json  tool_registry.json
#
# What this does NOT capture (covered elsewhere):
#   source code (git remote on GitHub)
#   references/  campaigns/<id>/  (dvc remote on TrueNAS via Phase 1.4)
#   venv/ caches (recreated by `pip install -r requirements.txt`)
#
# Idempotent. Safe to invoke from cron at any frequency.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-/mnt/nas-vault/Backups/ai-orchestrator}"
SNAPSHOT="$BACKUP_ROOT/snapshots/$(date -u +%Y%m%dT%H%M%SZ)"

if [ ! -d "$BACKUP_ROOT" ]; then
    echo "[backup] Backup root $BACKUP_ROOT not mounted/present — abort." >&2
    exit 2
fi

mkdir -p "$SNAPSHOT"

# --link-dest hard-links unchanged files against the previous snapshot
# (rsync incremental backup). Saves space + bandwidth dramatically once
# there's a baseline.
PREV="$(find "$BACKUP_ROOT/snapshots" -mindepth 1 -maxdepth 1 -type d | sort | tail -1 || true)"
LINKDEST_OPT=()
if [ -n "$PREV" ] && [ "$PREV" != "$SNAPSHOT" ]; then
    LINKDEST_OPT+=( --link-dest="$PREV" )
fi

# Sync each path explicitly so we don't accidentally pull in venv/, .git/,
# or DVC-managed dirs.
PATHS=(
    "memory"
    "vault"
    "projects"
    "config.json"
    ".env"
    "gates.json"
    "gates_log.json"
    "tool_registry.json"
)

for p in "${PATHS[@]}"; do
    src="$REPO_ROOT/$p"
    if [ ! -e "$src" ]; then
        echo "[backup] skip $p (not present)"
        continue
    fi
    if [ -d "$src" ]; then
        rsync -a --delete "${LINKDEST_OPT[@]}" "$src/" "$SNAPSHOT/$p/"
    else
        rsync -a "${LINKDEST_OPT[@]}" "$src" "$SNAPSHOT/$p"
    fi
done

# Update a 'latest' symlink so consumers (RESTORE.md scenario A) don't
# have to ls and pick.
ln -sfn "$SNAPSHOT" "$BACKUP_ROOT/latest"

du_total="$(du -sh --apparent-size "$SNAPSHOT" 2>/dev/null | cut -f1 || echo '?')"
echo "[backup] OK $SNAPSHOT ($du_total apparent)"
