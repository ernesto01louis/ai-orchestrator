#!/usr/bin/env bash
# install_tempo.sh — bootstrap a fresh Debian 12 LXC as the
# Phase 2.3 Grafana Tempo trace backend for the AI Orchestrator.
#
# Run inside the new LXC after `pct enter <id>`:
#     bash scripts/install_tempo.sh
#
# Optional env overrides:
#   TEMPO_VERSION   — defaults to a known-good release; bump to upgrade.
#   TEMPO_DATA_DIR  — defaults to /var/lib/tempo (block-storage backend).
#
# Idempotent: re-running upgrades the binary in place and rewrites the
# config file, but leaves the WAL + blocks under TEMPO_DATA_DIR alone.
#
# What this script does:
#   1. Installs sudo + curl + ca-certificates from base apt.
#   2. Downloads the Tempo binary tarball from grafana.com releases
#      and unpacks the static binary into /usr/local/bin/tempo.
#   3. Creates the `tempo` system user + data directories.
#   4. Writes /etc/tempo/tempo.yaml: OTLP/gRPC receiver on :4317,
#      OTLP/HTTP on :4318, query API on :3200, single-binary mode
#      with local block storage (no S3 / GCS — keeps deps minimal for
#      a homelab LXC).
#   5. Installs a systemd unit `tempo.service` and starts it.
#   6. Health-checks via /ready on :3200.
#
# Tempo's local-blocks backend is sufficient for the orchestrator's
# trace volume and avoids dragging in MinIO / S3. Retention rolls over
# at the cluster level (default 14d) — bump in tempo.yaml if needed.
set -euo pipefail

TEMPO_VERSION="${TEMPO_VERSION:-2.6.1}"
TEMPO_DATA_DIR="${TEMPO_DATA_DIR:-/var/lib/tempo}"

# 1. System deps. Tempo is a static Go binary so no extra runtime needed.
apt-get update
apt-get install -y sudo curl ca-certificates

# 2. Download + install the binary. The release archive is
# `tempo_<version>_linux_amd64.tar.gz`; the binary inside is just
# `tempo`. Idempotent: a re-run overwrites in place with the same
# checksum if the version hasn't changed.
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
TEMPO_URL="https://github.com/grafana/tempo/releases/download/v${TEMPO_VERSION}/tempo_${TEMPO_VERSION}_linux_amd64.tar.gz"
echo "Downloading Tempo ${TEMPO_VERSION}..."
curl -fsSL --retry 3 "$TEMPO_URL" -o "$TMPDIR/tempo.tar.gz"
tar -xzf "$TMPDIR/tempo.tar.gz" -C "$TMPDIR"
install -m 0755 "$TMPDIR/tempo" /usr/local/bin/tempo

# 3. tempo user + data dirs (idempotent).
if ! id -u tempo >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin tempo
fi
install -d -m 0750 -o tempo -g tempo "$TEMPO_DATA_DIR"
install -d -m 0750 -o tempo -g tempo "$TEMPO_DATA_DIR/wal"
install -d -m 0750 -o tempo -g tempo "$TEMPO_DATA_DIR/blocks"
install -d -m 0750 /etc/tempo

# 4. Tempo config — single-binary mode, OTLP receiver on :4317,
# query on :3200, local-blocks storage backend.
cat > /etc/tempo/tempo.yaml <<EOF
# tempo.yaml — single-binary Tempo for the AI Orchestrator homelab.
# Managed by scripts/install_tempo.sh. Manual edits will survive re-run
# only if you also bump TEMPO_VERSION (the script overwrites unconditionally).

server:
  http_listen_port: 3200
  grpc_listen_port: 9095
  log_level: info

distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318

ingester:
  trace_idle_period: 10s
  max_block_duration: 5m

compactor:
  compaction:
    block_retention: 336h  # 14 days

metrics_generator:
  storage:
    path: ${TEMPO_DATA_DIR}/generator/wal
    remote_write: []

storage:
  trace:
    backend: local
    local:
      path: ${TEMPO_DATA_DIR}/blocks
    wal:
      path: ${TEMPO_DATA_DIR}/wal

usage_report:
  reporting_enabled: false
EOF
chown -R tempo:tempo /etc/tempo

# 5. systemd unit. Restart=on-failure so a transient blip doesn't
# require manual intervention. NoNewPrivileges + PrivateTmp tighten
# the blast radius without breaking the local-blocks backend.
cat > /etc/systemd/system/tempo.service <<EOF
[Unit]
Description=Grafana Tempo (AI Orchestrator trace backend)
Documentation=https://grafana.com/docs/tempo/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tempo
Group=tempo
ExecStart=/usr/local/bin/tempo -config.file=/etc/tempo/tempo.yaml
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now tempo.service
sleep 5

# 6. Health check. /ready returns 200 once Tempo is ingesting.
if ! curl -fsS http://127.0.0.1:3200/ready >/dev/null; then
    echo "ERROR: Tempo did not become ready on :3200 within 5s." >&2
    journalctl -u tempo.service --no-pager -n 30 >&2
    exit 1
fi

echo
echo "Done. Tempo ${TEMPO_VERSION} is up. From the orchestrator LXC,"
echo "set the OTLP endpoint in .env:"
echo
echo "    OTEL_ENDPOINT=$(hostname -I | awk '{print $1}'):4317"
echo
echo "Then flip \`otel.enabled=true\` in config.json and restart the"
echo "orchestrator service. Verify traces flow with:"
echo
echo "    curl http://$(hostname -I | awk '{print $1}'):3200/api/search"
echo
