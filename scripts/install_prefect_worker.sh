#!/usr/bin/env bash
# install_prefect_worker.sh — install Prefect worker on the orchestrator LXC.
# Run from /opt/ai-orchestrator (orchestrator's checkout) after the server LXC
# is up and reachable.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/ai-orchestrator}"
PREFECT_API_URL="${PREFECT_API_URL:-}"

if [[ -z "$PREFECT_API_URL" ]]; then
    echo "ERROR: PREFECT_API_URL not set."
    echo "Example: export PREFECT_API_URL=http://prefect.tailnet:4200/api"
    exit 1
fi

# Use the orchestrator's existing venv (Prefect already installed via requirements.txt)
VENV="$REPO_ROOT/venv"
if [[ ! -x "$VENV/bin/prefect" ]]; then
    echo "ERROR: prefect not found in $VENV/bin — run pip install -r requirements.txt first."
    exit 1
fi

# Configure Prefect client to point at the server
"$VENV/bin/prefect" config set PREFECT_API_URL="$PREFECT_API_URL"

# Apply deployments
cd "$REPO_ROOT"
"$VENV/bin/prefect" deploy --all

# Systemd unit for the worker
cat > /etc/systemd/system/prefect-worker.service <<EOF
[Unit]
Description=Prefect 3.x Worker for ai-orchestrator
After=network-online.target ai-orchestrator.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
Environment=PREFECT_API_URL=$PREFECT_API_URL
ExecStart=$VENV/bin/prefect worker start --pool orchestrator-pool
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable prefect-worker.service
echo "prefect-worker.service installed and enabled. Start it ONLY when execution_mode=deployment."
echo "  systemctl start prefect-worker.service"
