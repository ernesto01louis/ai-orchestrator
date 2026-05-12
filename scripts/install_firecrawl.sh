#!/usr/bin/env bash
# Repo-screening spike (2026-05-12) — provision the firecrawl-server LXC
# for the web reference ingestion primitive.
#
# Runs ON THE PROXMOX HOST (not on LXC 200). Provisions LXC 206 via
# pct create, installs Docker + Docker Compose, clones the firecrawl
# repo, brings up the self-host compose stack (api / playwright /
# redis / rabbitmq / nuq-postgres).
#
# Idempotent: skips pct create when VMID 206 already exists; re-runs
# the in-LXC bootstrap regardless (apt-get install is a no-op on
# already-installed packages, docker compose up -d is the desired
# "ensure running" semantic).
#
# Resources: 4 cores / 8 GB RAM / 1 GB swap / 30 GB rootfs on local-lvm.
# The firecrawl docker-compose declares 12 GB across api + playwright
# under load; 8 GB has been sufficient for the spike's smoke tests
# but raise to 12 GB if production-scale ingest hits OOM.
#
# Once running, flip web_ingest.enabled=true in config.json on LXC 200
# and restart ai-orchestrator.service. The startup healthcheck logs
# "firecrawl: reachable" on success.
set -euo pipefail

VMID="${VMID:-206}"
HOSTNAME="${HOSTNAME:-firecrawl-server}"
IP="${IP:-192.168.2.189/24}"
GW="${GW:-192.168.2.1}"
CORES="${CORES:-4}"
MEMORY="${MEMORY:-8192}"
SWAP="${SWAP:-1024}"
ROOTFS_SIZE="${ROOTFS_SIZE:-30}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-local-lvm}"
TEMPLATE="${TEMPLATE:-local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst}"
FIRECRAWL_REPO="${FIRECRAWL_REPO:-https://github.com/firecrawl/firecrawl.git}"

if [ "$(id -u)" -ne 0 ]; then
    echo "[install_firecrawl] must run as root on the Proxmox host" >&2
    exit 1
fi

if ! command -v pct >/dev/null 2>&1; then
    echo "[install_firecrawl] pct not found — are you on the Proxmox host?" >&2
    exit 1
fi

if pct status "$VMID" >/dev/null 2>&1; then
    echo "[install_firecrawl] VMID $VMID already exists — skipping pct create"
else
    PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
    BACKUP_DIR="${BACKUP_DIR:-/root}"
    PASSWORD_FILE="$BACKUP_DIR/.firecrawl-lxc-root-password"
    echo "$PASSWORD" > "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"
    echo "[install_firecrawl] LXC root password backed up to $PASSWORD_FILE"

    pct create "$VMID" "$TEMPLATE" \
        --hostname "$HOSTNAME" \
        --cores "$CORES" \
        --memory "$MEMORY" \
        --swap "$SWAP" \
        --rootfs "${ROOTFS_STORAGE}:${ROOTFS_SIZE}" \
        --net0 "name=eth0,bridge=vmbr0,gw=${GW},ip=${IP},type=veth" \
        --nameserver "$GW" \
        --ostype debian \
        --features "nesting=1,keyctl=1" \
        --unprivileged 1 \
        --onboot 1 \
        --password "$PASSWORD"

    pct start "$VMID"
    sleep 3
fi

if ! pct status "$VMID" | grep -q "running"; then
    pct start "$VMID"
    sleep 3
fi

# Install Docker + compose in the LXC. Apt operations are idempotent.
pct exec "$VMID" -- bash -c '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
'

# Clone firecrawl repo (skip if already present) and bring up compose.
pct exec "$VMID" -- bash -c "
set -euo pipefail
if [ ! -d /opt/firecrawl/.git ]; then
    git clone --depth 1 '$FIRECRAWL_REPO' /opt/firecrawl
fi
cd /opt/firecrawl
if [ ! -f .env ]; then
    cp apps/api/.env.example .env
    sed -i 's/USE_DB_AUTHENTICATION=true/USE_DB_AUTHENTICATION=false/' .env
fi
docker compose up -d --build
"

# Probe from inside the LXC; cheap sanity check.
pct exec "$VMID" -- bash -c '
for _ in $(seq 1 30); do
    if curl -sS --max-time 2 http://127.0.0.1:3002/ >/dev/null; then
        echo "[install_firecrawl] firecrawl-api responding on :3002"
        exit 0
    fi
    sleep 2
done
echo "[install_firecrawl] WARNING: firecrawl-api did not respond within 60s" >&2
exit 1
'

cat <<EOF

[install_firecrawl] bring-up complete. Next steps on LXC 200:

  1. Confirm reachability:
       curl -sS http://${IP%/*}:3002/

  2. Flip the gate in /opt/ai-orchestrator/config.json:
       "web_ingest": { "enabled": true }

  3. Restart the orchestrator:
       systemctl restart ai-orchestrator.service

  4. Watch for "firecrawl: reachable" in the orchestrator startup log.

EOF
