#!/usr/bin/env bash
# Install the ai-orchestrator-backup systemd timer + service unit.
#
# Idempotent. Re-runs are safe. Operators invoke once after pulling
# main; the units land under /etc/systemd/system and the timer is
# enabled + started immediately.
#
# Verify with:
#   systemctl status ai-orchestrator-backup.timer
#   systemctl list-timers ai-orchestrator-backup.timer
#
# Fast-forward (skip the schedule and run NOW):
#   systemctl start ai-orchestrator-backup.service
#   journalctl -u ai-orchestrator-backup.service -n 50

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="$REPO_ROOT/scripts/systemd"
DST_DIR="/etc/systemd/system"

if [ "$EUID" -ne 0 ]; then
    echo "[install] Must run as root (writing to $DST_DIR)" >&2
    exit 1
fi

for unit in ai-orchestrator-backup.service ai-orchestrator-backup.timer; do
    src="$SRC_DIR/$unit"
    dst="$DST_DIR/$unit"
    if [ ! -f "$src" ]; then
        echo "[install] Missing template: $src" >&2
        exit 2
    fi
    install -m 0644 "$src" "$dst"
    echo "[install] $dst"
done

systemctl daemon-reload
systemctl enable --now ai-orchestrator-backup.timer

echo "[install] Done. Next fire:"
systemctl list-timers ai-orchestrator-backup.timer --no-pager
