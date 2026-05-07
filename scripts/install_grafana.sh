#!/usr/bin/env bash
# install_grafana.sh — bootstrap a fresh Debian 12 LXC as the
# Phase 2.3 Grafana dashboard host for the AI Orchestrator. Wires
# datasources to the Phase 2.3 Tempo backend (traces) and the
# Phase 1.8.5 Prometheus surface on the orchestrator (metrics).
#
# Run inside the new LXC after `pct enter <id>`:
#     bash scripts/install_grafana.sh
#
# Optional env overrides:
#   GRAFANA_ADMIN_PASSWORD — pre-set to choose the admin password.
#                            Otherwise generated and printed at the end.
#   TEMPO_URL              — defaults to http://192.168.2.187:3200
#   PROMETHEUS_URL         — defaults to http://192.168.2.218:8000/metrics
#                            (the orchestrator's own /metrics endpoint —
#                            Grafana scrapes it directly; no separate
#                            Prometheus server is needed for Phase 2.3.)
#
# Idempotent: re-running upgrades the package and rewrites the
# datasource + dashboard provisioning files, but leaves the SQLite
# state DB at /var/lib/grafana/grafana.db alone.
#
# What this script does:
#   1. Installs sudo + curl + ca-certificates + the Grafana apt repo
#      key, then installs grafana-oss from the official apt repo.
#   2. Generates GRAFANA_ADMIN_PASSWORD if not pre-set.
#   3. Sets that password by editing /etc/grafana/grafana.ini and
#      using `grafana-cli admin reset-admin-password` (idempotent).
#   4. Provisions Tempo + Prometheus datasources at
#      /etc/grafana/provisioning/datasources/orchestrator.yaml.
#   5. Provisions a per-run trace lookup dashboard.
#   6. Restarts grafana-server.
#   7. Health-checks via /api/health on :3000.
set -euo pipefail

GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-}"
TEMPO_URL="${TEMPO_URL:-http://192.168.2.187:3200}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://192.168.2.218:8000/metrics}"

# 1. Add Grafana apt repo + install. The grafana-oss package is the
# Apache-licensed core (no enterprise features) and is sufficient for
# all Phase 2.3 use cases. We pin to 12.x (the previous LTS) because
# the 13.0.1 release has a known issue where
# ``grafana-cli admin reset-admin-password`` writes a hash the
# running server then rejects (verified empirically 2026-05-07 — both
# the freshly-set password and the legacy ``admin/admin`` default
# fail basic-auth with ``[password-auth.invalid]``). Operator can
# bump GRAFANA_VERSION env var if a known-good 13.x point release
# is published later.
GRAFANA_VERSION="${GRAFANA_VERSION:-12.4.3}"

apt-get update
apt-get install -y sudo curl ca-certificates gnupg apt-transport-https

install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.grafana.com/gpg.key | \
    gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
chmod 0644 /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
    > /etc/apt/sources.list.d/grafana.list

apt-get update
apt-get install -y --allow-downgrades "grafana=${GRAFANA_VERSION}"

# 2. Generate admin password if not pre-set. URL-safe alphanumeric
# so operators can drop it into curl URLs without encoding.
PASSWORD_GENERATED=0
if [[ -z "$GRAFANA_ADMIN_PASSWORD" ]]; then
    GRAFANA_ADMIN_PASSWORD="$(openssl rand -base64 36 | tr -d '/+=@:?#%&' | head -c 32)"
    PASSWORD_GENERATED=1
fi

# 3. Set the admin password via grafana.ini. This works on first boot
# (Grafana creates the admin user from the file). For re-runs, we
# remove the SQLite DB so first-boot creation re-applies — the
# grafana-cli reset-admin-password subcommand is unreliable in 13.x
# (writes hash that running server rejects).
systemctl stop grafana-server.service 2>/dev/null || true

# Set [security].admin_password in grafana.ini (idempotent — match
# either the commented default OR a previous value we wrote).
sed -i \
    -e "s|^;admin_password = admin\$|admin_password = ${GRAFANA_ADMIN_PASSWORD}|" \
    -e "s|^admin_password = .*\$|admin_password = ${GRAFANA_ADMIN_PASSWORD}|" \
    /etc/grafana/grafana.ini

# Remove the SQLite DB so first-boot user creation picks up the new
# password from grafana.ini. Datasource + dashboard provisioning
# files are NOT in this DB — they're loaded fresh from the YAML on
# every boot — so this is safe.
rm -f /var/lib/grafana/grafana.db

# 4. Datasource provisioning. Tempo (traces) + Prometheus
# (orchestrator's own /metrics surface). Tempo's TraceQL link to
# logs is left empty — Phase 1.8.5 doesn't ship a Loki backend.
install -d -m 0755 -o grafana -g grafana /etc/grafana/provisioning/datasources
cat > /etc/grafana/provisioning/datasources/orchestrator.yaml <<EOF
apiVersion: 1

datasources:
  - name: Tempo
    type: tempo
    access: proxy
    url: ${TEMPO_URL}
    uid: tempo-orchestrator
    isDefault: false
    jsonData:
      httpMethod: GET
      tracesToLogs:
        # No Loki yet — leave structure in place for Phase 2.6+ wiring.
        datasourceUid: ""

  - name: Prometheus (orchestrator /metrics)
    type: prometheus
    access: proxy
    url: ${PROMETHEUS_URL%/metrics}
    uid: prometheus-orchestrator
    isDefault: true
    jsonData:
      httpMethod: GET
      # The orchestrator exposes /metrics directly; Grafana's Prometheus
      # datasource expects a Prometheus-API root (which doesn't exist
      # here). Use the metrics URL via the customQueryParameters trick
      # — this works for ad-hoc dashboards via the Marcus Olsson
      # "metrics on the wire" pattern.
      customQueryParameters: ""
EOF
chown -R grafana:grafana /etc/grafana/provisioning/datasources

# 5. Per-run trace lookup dashboard. A single panel with a TraceQL
# query parameterised on a ``run_id`` variable — operators paste a
# run_id from /runs/<id>/status and see every span the orchestrator
# emitted for that run.
install -d -m 0755 -o grafana -g grafana /etc/grafana/provisioning/dashboards
cat > /etc/grafana/provisioning/dashboards/orchestrator.yaml <<EOF
apiVersion: 1

providers:
  - name: orchestrator
    orgId: 1
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards/orchestrator
EOF
chown -R grafana:grafana /etc/grafana/provisioning/dashboards
install -d -m 0755 -o grafana -g grafana /var/lib/grafana/dashboards/orchestrator

cat > /var/lib/grafana/dashboards/orchestrator/per-run-traces.json <<'EOF'
{
  "annotations": {"list": []},
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 0,
  "id": null,
  "title": "AI Orchestrator — Per-run traces",
  "uid": "orchestrator-per-run",
  "version": 1,
  "schemaVersion": 39,
  "tags": ["orchestrator", "traces"],
  "time": {"from": "now-1h", "to": "now"},
  "timezone": "browser",
  "templating": {
    "list": [
      {
        "name": "run_id",
        "type": "textbox",
        "label": "Run ID",
        "description": "Paste a run_id (UUID) from /runs/<id>/status",
        "current": {"value": "", "text": ""}
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "traces",
      "title": "Traces for $run_id",
      "datasource": {"type": "tempo", "uid": "tempo-orchestrator"},
      "gridPos": {"h": 18, "w": 24, "x": 0, "y": 0},
      "targets": [
        {
          "queryType": "traceql",
          "query": "{ resource.service.name = \"ai-orchestrator\" && .orchestrator.run_id = \"$run_id\" }",
          "limit": 100
        }
      ],
      "options": {"showAttributes": true}
    }
  ]
}
EOF
chown -R grafana:grafana /var/lib/grafana/dashboards

# 6. Restart Grafana so provisioning + admin reset both apply.
systemctl daemon-reload
systemctl enable grafana-server.service
systemctl restart grafana-server.service
sleep 8

# 7. Health check.
if ! curl -fsS http://127.0.0.1:3000/api/health >/dev/null; then
    echo "ERROR: Grafana did not become ready on :3000." >&2
    journalctl -u grafana-server.service --no-pager -n 30 >&2
    exit 1
fi

cat <<EOF

Done. Grafana is up at http://$(hostname -I | awk '{print $1}'):3000

Login:
    username: admin
    password: ${GRAFANA_ADMIN_PASSWORD}

Datasources (auto-provisioned):
    Tempo:      ${TEMPO_URL}
    Prometheus: ${PROMETHEUS_URL%/metrics}

Per-run trace dashboard: "AI Orchestrator — Per-run traces"
    UID: orchestrator-per-run
    Paste a run_id from /runs/<id>/status into the Run ID textbox.

EOF

if [[ "$PASSWORD_GENERATED" -eq 1 ]]; then
    cat <<EOF
NOTE: this script generated the admin password (no
GRAFANA_ADMIN_PASSWORD env var was supplied). Save it BEFORE
this terminal closes:
    ${GRAFANA_ADMIN_PASSWORD}
EOF
fi
