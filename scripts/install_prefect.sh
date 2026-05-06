#!/usr/bin/env bash
# install_prefect.sh — bootstrap a fresh Debian 12 LXC as the Prefect server.
#
# Run inside the new LXC after `pct enter <id>`:
#     bash scripts/install_prefect.sh
#
# Idempotent: re-running upgrades pip + Prefect, leaves the systemd unit alone.
set -euo pipefail

# 1. System deps
apt-get update
apt-get install -y python3-venv python3-pip curl ca-certificates

# 2. Create a dedicated venv
mkdir -p /opt/prefect
python3 -m venv /opt/prefect/venv
/opt/prefect/venv/bin/pip install --upgrade pip
/opt/prefect/venv/bin/pip install 'prefect>=3.0,<4'

# 3. SQLite database under /var/lib/prefect (default location is fine,
#    but pin it explicitly so backups and Phase 2.1 Postgres swap are easy).
mkdir -p /var/lib/prefect
export PREFECT_HOME=/var/lib/prefect

# 4. Systemd unit
cat > /etc/systemd/system/prefect-server.service <<'EOF'
[Unit]
Description=Prefect 3.x Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PREFECT_HOME=/var/lib/prefect
ExecStart=/opt/prefect/venv/bin/prefect server start --host 0.0.0.0 --port 4200
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now prefect-server.service

# 5. Wait for it to come up
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:4200/api/health > /dev/null 2>&1; then
        echo "Prefect server up on :4200"
        break
    fi
    sleep 1
done

# 6. Create the work pool
/opt/prefect/venv/bin/prefect work-pool create --type process orchestrator-pool || true

echo
echo "Done. Tailscale ACL: ensure this LXC is reachable from the orchestrator LXC on tcp:4200."
echo "On the orchestrator LXC, run:  bash scripts/install_prefect_worker.sh"
