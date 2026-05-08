# Runbook

Operational tasks for the orchestrator running on LXC 192.168.2.218.

## Service control

```bash
systemctl status   ai-orchestrator
systemctl restart  ai-orchestrator
systemctl stop     ai-orchestrator
journalctl -u ai-orchestrator -f       # follow logs
journalctl -u ai-orchestrator --since "10 min ago"
```

The unit lives at `/etc/systemd/system/ai-orchestrator.service` and runs
`uvicorn app:app --host 0.0.0.0 --port 8000` from `/opt/ai-orchestrator/venv/`.

## Health check

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```

Aggregates: orchestrator load + Ollama main/judge reachability + Hindsight reachability.

## Triggering a run via curl

```bash
curl -sX POST http://127.0.0.1:8000/orchestrate \
  -H 'Content-Type: application/json' \
  -d '{
    "project_name": "smoke",
    "prompt": "write a python script that prints hello",
    "planner_model": "qwen2.5:72b",
    "generator_models": ["qwen2.5-coder:32b"],
    "judge_model": "qwen2.5:72b",
    "deploy_target": "pi-2"
  }'
```

Watch live: `wscat -c ws://127.0.0.1:8000/ws` (or open `/ui` in a browser).

## Add an SSH target

1. Append to `config.json` under `ssh_targets`:
   ```json
   {"name": "newhost", "host": "192.168.2.X", "username": "louis", "key_path": "/root/.ssh/id_ed25519_dvc"}
   ```
2. Make sure the key is authorized on the new host:
   ```bash
   ssh-copy-id -i /root/.ssh/id_ed25519_dvc.pub louis@192.168.2.X
   ```
3. `systemctl restart ai-orchestrator`.
4. `curl http://127.0.0.1:8000/targets` to confirm it's listed.

## Add a tool

Edit `tool_registry.json`, then `curl -X POST http://127.0.0.1:8000/agents/reload`
(or just restart the service — the tool registry is loaded fresh per
run via `tools.load_tool_registry`).

## Rotate the Gotify token

1. Open Gotify admin at `http://192.168.2.203:8090`, regenerate the application token.
2. Edit `/opt/ai-orchestrator/.env`, replace `GOTIFY_TOKEN=`.
3. `systemctl restart ai-orchestrator`.
4. `curl -sX POST http://127.0.0.1:8000/notifications/test` to confirm.

## Restore from backup

The orchestrator's stateful paths (`memory/`, `vault/`, `references/`,
`projects/`, `logs/`, `config.json`, `.env`, `gates.json`) are
gitignored and need a separate backup channel (currently rsync to
TrueNAS — see Phase 0.4).

To restore:

1. Stop the service: `systemctl stop ai-orchestrator`.
2. Restore the data dirs from the backup tarball/rsync target.
3. `git pull` to make sure code matches the data version.
4. `pip install -r requirements.txt` in venv.
5. `systemctl start ai-orchestrator`.
6. `curl http://127.0.0.1:8000/health` — should be green.

## Pause / resume

```bash
curl -sX POST http://127.0.0.1:8000/control/pause
curl -sX POST http://127.0.0.1:8000/control/restart
curl -s http://127.0.0.1:8000/control/status
```

`pause` blocks new runs but lets in-flight runs finish.

## Evidence-bundle signing key

Phase 1.2 evidence bundles are signed with an Ed25519 key at
`/etc/ai-orchestrator/signing/`. First-time install:

```bash
bash scripts/install_signing_key.sh
ls -la /etc/ai-orchestrator/signing/
# ed25519.seed  chmod 600 (PRIVATE — never copy off-host)
# ed25519.pub   chmod 644 (public, ships with bundles)
```

The script is idempotent — re-running prints the keyid and exits.
Use `--force` to **rotate**, but be aware: rotation invalidates every
previously-signed bundle. The signing module expects a single
host-wide key; per-user / per-tenant scoping is a Phase 2.x decision.

To fetch a campaign's signed evidence:

```bash
# JSON
curl -s http://127.0.0.1:8000/campaigns/<id>/evidence | jq

# Full RO-Crate (zip — drop into a paper appendix)
curl -O http://127.0.0.1:8000/campaigns/<id>/evidence.crate.zip
unzip campaign-*.crate.zip -d bundle/
python -m evidence.verify --crate-dir bundle/
# → "OK  crate at … verifies cleanly"

# Recompute + verify in-place via API (no Python needed)
curl -s http://127.0.0.1:8000/campaigns/<id>/evidence/verify | jq
```

If the verify route returns errors, the bundle has been tampered with
(or the manifest is stale). Re-emit:

```bash
curl -sX POST http://127.0.0.1:8000/campaigns/<id>/evidence/refresh
```

## Bearer-token authentication (Phase 1.7)

Disabled by default. To turn it on, set the env var
`ORCHESTRATOR_API_TOKEN` in `.env` to a high-entropy value:

```bash
echo "ORCHESTRATOR_API_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> /opt/ai-orchestrator/.env
systemctl restart ai-orchestrator
```

When the env var is **unset or empty**, the middleware is a no-op and
every request is allowed (backward-compatible with pre-1.7 deployments).

When set, every HTTP and WebSocket request must carry
`Authorization: Bearer <token>` except:

- `GET /health` — for liveness probes
- `GET /openapi.json`, `GET /docs`, `GET /redoc`, `GET /docs/oauth2-redirect`
- HTTP `OPTIONS` requests (CORS preflight)

Verify:

```bash
TOKEN="<your-token>"

# Without header → 401
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/control/status
# 401

# With header → 200
curl -sS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/control/status
```

Python client SDK (Phase 1.6) — auth shape is forward-compatible:

```python
from ai_orchestrator_client import OrchestratorClient, BearerTokenAuth

client = OrchestratorClient(
    base_url="http://orchestrator:8000",
    auth=BearerTokenAuth(token=os.environ["ORCHESTRATOR_API_TOKEN"]),
)
```

WebSocket clients pass the header during the handshake:

```python
import websockets
async with websockets.connect(
    "ws://orchestrator:8000/ws",
    additional_headers={"Authorization": f"Bearer {token}"},
) as ws:
    ...
```

Failed WebSocket auth is rejected during the upgrade handshake. The
server sends a pre-accept ``websocket.close`` which uvicorn translates
to HTTP 403 on the upgrade response, so the websockets-13 client raises
``InvalidStatusCode(403)``. (Internal close code 4401 is set by the
middleware but is dropped by the WS-to-HTTP rejection translation —
clients see the 403, not the 4401.)

The MCP endpoint at `/mcp` is covered by the same middleware. External
MCP clients pass the header on the streamable HTTP connection — see
`docs/MCP_TOOLS.md` for the discovery contract.

### Token rotation

Generate a new token, restart the service, update consumers. There's no
in-process rotation hook yet; if that becomes painful, see Phase 2.x in
`ROADMAP.md`.

## Run the test suite

```bash
cd /opt/ai-orchestrator
venv/bin/pytest -q
```

Tests assume the orchestrator is running on `127.0.0.1:8000`. HTTP tests
auto-skip if `/health` is unreachable.

## Common deploy issues

- **Planner returns empty** → check Ollama judge reachability (`/health`).
  `curl http://192.168.2.219:11434/api/tags`.
- **All generators fail** → check Ollama main; consider a smaller model.
  `GET /model-stats` for per-model fail rates.
- **WebSocket no live events** → was a known bug pre-0.e; if it recurs,
  check `core/runtime.py:_MAIN_LOOP` is set in lifespan.
- **Vault sync failures** → SSH key path in `config.json` must match
  what's on the Hindsight host (192.168.2.203). Tail
  `journalctl -u ai-orchestrator | grep vault`.

## Prefect operations (Phase 1.3)

### Topology
- Prefect server: LXC 201 (`prefect-server`), LAN 192.168.2.182:4200, Tailscale 100.76.57.6:4200
- Orchestrator: LXC 200 (`ai-orchestrator`), LAN 192.168.2.* — `config.json` `prefect.api_url` points at the Prefect LAN IP
- Worker (deployment mode only): `prefect-worker.service` on LXC 200 — installed by `scripts/install_prefect_worker.sh` but `enabled` only; `start` it manually when switching to deployment mode

### Healthchecks
```bash
# From the orchestrator host
curl -fsS http://192.168.2.182:4200/api/health && echo OK
# On the orchestrator LXC
PREFECT_API_URL=http://192.168.2.182:4200/api venv/bin/python -c \
  "from prefect_io import _healthcheck; print(_healthcheck())"
# Run real-server tests
PREFECT_API_URL=http://192.168.2.182:4200/api venv/bin/pytest -q -m prefect_real
```

### Restarting the Prefect server
```bash
ssh root@192.168.2.13 "pct exec 201 -- systemctl restart prefect-server.service"
```

### Switching to deployment mode (on the orchestrator LXC)
```bash
# 1. Start the worker
systemctl start prefect-worker.service
# 2. Edit config
#    Set "prefect.execution_mode": "deployment" in /opt/ai-orchestrator/config.json
# 3. Restart the orchestrator
systemctl restart ai-orchestrator
```

### Switching back to in_process mode
```bash
# 1. Edit config
#    Set "prefect.execution_mode": "in_process" in /opt/ai-orchestrator/config.json
# 2. Restart the orchestrator
systemctl restart ai-orchestrator
# 3. Stop the worker (optional; idle worker is harmless)
systemctl stop prefect-worker.service
```

### Re-registering deployments after code changes
```bash
cd /opt/ai-orchestrator
PREFECT_API_URL=http://192.168.2.182:4200/api venv/bin/prefect deploy --all
```

### Flushing the SQLite database (rare, e.g. corruption)
```bash
ssh root@192.168.2.13 "pct exec 201 -- bash -c '
    systemctl stop prefect-server
    rm -f /var/lib/prefect/prefect.db
    systemctl start prefect-server
'"
# Then re-register deployments from the orchestrator (see above)
```

### Server-down behavior
- Logs at orchestrator: "Prefect server unreachable; using daemon-thread fallback"
- `_healthcheck()` returns False at startup → printed warning to stdout
- Runs continue without Prefect UI tracking until server returns
- WebSocket UI keeps working because inline `_update_run_status(...)` calls
  in `run_orchestration`/`run_campaign` are preserved as belt-and-suspenders

## Data versioning with DVC (Phase 1.4)

The orchestrator pushes large binary artifacts (RAG references, per-campaign
evidence crates) to a DVC remote on TrueNAS over SSH. Source tree stays in
git; data lives on the NAS pool. Tracking files (`*.dvc`) are committed so
any clone can `dvc pull` to reconstruct the working tree.

### Topology

| Component | Where | Notes |
|---|---|---|
| DVC client | LXC 200 (this host) | `pip install 'dvc[ssh]'` already in `requirements.txt` |
| Remote name | `truenas` (default) | configured in `.dvc/config` |
| Remote URL | `ssh://dvc-orchestrator@192.168.2.222/mnt/f3/orchestrator-dvc` | adjust if pool/path differs |
| SSH key | `/root/.ssh/id_ed25519_dvc` | set via `dvc remote modify truenas keyfile` |
| TrueNAS user | `dvc-orchestrator` | dedicated, NOT root |

### One-time TrueNAS setup
1. TrueNAS UI → System Settings → Services → SSH → enable + start.
2. TrueNAS UI → Credentials → Local Users → Add: username `dvc-orchestrator`,
   home `/mnt/f3/dvc-orch-home/dvc-orchestrator` (TrueNAS nests under the
   parent dataset by default), primary group `dvc-orchestrator`,
   SSH public key = contents of `/root/.ssh/id_ed25519_dvc.pub` from LXC 200.
3. TrueNAS UI → Datasets → create dataset `f3/orchestrator-dvc` owned
   by `dvc-orchestrator:dvc-orchestrator`, mode `0750`.
4. From LXC 200: `ssh dvc-orchestrator@192.168.2.222 'ls /mnt/f3/orchestrator-dvc'`
   should succeed without password prompt.

### Tracking the bulky directories

`scripts/dvc_track.sh` is the one-shot entry point — it `dvc add`s
`references/` and `campaigns/` (the two ROADMAP-1.4 paths) and pushes
them to the remote. Re-run any time after a campaign finishes:

```bash
cd /opt/ai-orchestrator
source venv/bin/activate
./scripts/dvc_track.sh
git add references.dvc campaigns.dvc
git commit -m "dvc: snapshot references + campaigns @ $(date -u +%Y-%m-%d)"
git push
```

Override the path list via env: `PATHS="datasets references" ./scripts/dvc_track.sh`.

### Pulling on a fresh clone

```bash
git clone https://github.com/ernesto01louis/ai-orchestrator.git
cd ai-orchestrator
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
dvc pull              # restores references/ + campaigns/ from TrueNAS
```

### Pre-commit hook (OPT-IN)

A pre-commit hook script that blocks commits when DVC artifacts haven't been
pushed lives at `scripts/git-hooks-available/pre-commit-dvc-status`. It is
NOT activated by default — pre-commit hooks add friction every commit, and
this one is only useful if you actively work with DVC-tracked data.

To activate per-clone:

```bash
ln -s ../../scripts/git-hooks-available/pre-commit-dvc-status \
       .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

To deactivate later: `rm .git/hooks/pre-commit`.

### Common DVC issues

- `Permission denied (publickey)` from `dvc push` → SSH key not in
  `dvc-orchestrator@truenas:.ssh/authorized_keys`, or `~/.ssh/` perms are
  too permissive (sshd `StrictModes` is on). Fix in TrueNAS Web Shell:
  ```
  sudo chmod 700 /mnt/f3/dvc-orch-home/dvc-orchestrator
  sudo chmod 700 /mnt/f3/dvc-orch-home/dvc-orchestrator/.ssh
  sudo chmod 600 /mnt/f3/dvc-orch-home/dvc-orchestrator/.ssh/authorized_keys
  ```
  TrueNAS dataset ACLs default to `0777` after creation — re-apply the
  modes any time you recreate the user or recursively reset permissions
  on the parent dataset.
- `Password change required but no TTY available` after first login →
  TrueNAS marks new users' passwords as expired. In the UI, edit the
  user and tick **Disable Password** (key-only auth, skips PAM expiry).
- `dvc remote default` shows wrong URL → edit `.dvc/config` directly or
  `dvc remote modify truenas url ssh://...`.
- `dvc status` reports "missing" files after `git pull` → run `dvc pull` to
  restore the working tree from TrueNAS.

## Verifying run integrity (Phase 1.5)

Every successful run automatically writes a SHA256 manifest, and every
successful campaign rolls those up into a Merkle root. These are passive
— no operator action needed for day-to-day use.

### Where the files live

- **Per-run manifest** — `projects/<project>/runs/<run_id>/manifest.json`  
  Written at the end of every successful run. Contains SHA256 + size for
  every artifact in the run directory. Symlinks are skipped; the manifest
  file itself is excluded from its own hash set.

- **Campaign Merkle root** — `campaigns/<campaign_id>/merkle.json`  
  Written at the end of every successful campaign. Rolls up each run's
  manifest hash into a single Merkle root, giving tamper-evident
  campaign-level integrity in one value.

### CLI verification

After `pip install -e .` (or in the activated venv):

```bash
# Verify a single run
orchestrator verify-run <run_id>
orchestrator verify-campaign <campaign_id>

# Or directly without install:
python -m cli.main verify-run <run_id>
python -m cli.main verify-campaign <campaign_id>

# Override default paths:
orchestrator verify-run <run_id> --projects-dir /path/to/projects
orchestrator verify-campaign <camp_id> --campaigns-dir /path/to/campaigns
```

Exit 0 = ok; exit 1 = corrupted, missing, or not found.

### HTTP verification

```bash
curl http://127.0.0.1:8000/runs/<run_id>/verify
curl http://127.0.0.1:8000/campaigns/<campaign_id>/verify-merkle

# manifest_status is also included in the standard status endpoint
# (lazy-computed on first read for completed runs, cached after):
curl http://127.0.0.1:8000/status/<run_id>
```

**Run verify** (`/runs/<run_id>/verify`) returns:

```json
{"run_id": "...", "valid": true, "status": "ok", "mismatches": []}
```

**Campaign verify** (`/campaigns/<campaign_id>/verify-merkle`) returns:

```json
{"campaign_id": "...", "valid": true, "status": "ok", "mismatches": []}
```

**Status endpoint** (`/status/<run_id>`) is not a verify-result envelope — it
returns the existing status response shape with `manifest_status` added as a
new field (lazy-computed on first read for completed runs, cached after).

HTTP 200 always — mismatches are domain-level errors, not HTTP errors.
HTTP 404 only when the run/campaign ID itself is unknown.

### Status values

| Status | Meaning |
|---|---|
| `ok` | All tracked files match their recorded SHA256 + size. |
| `corrupted` | At least one file's SHA256 differs from the manifest, or files are missing/extra on disk vs. what the manifest recorded. |
| `missing` | The manifest file does not exist (run predates Phase 1.5, or was never written). |
| `skipped` | Manifest write failed at end-of-run/campaign (transient failure), or verify itself raised on a filesystem error. Treat as "not verified" rather than "corruption confirmed". |

## Phase 1.5 first-time DVC snapshot

Phase 1.5 took the one-shot bulk DVC snapshot of `references/` and
`campaigns/` to TrueNAS:

```bash
# In the activated venv on LXC 200:
bash scripts/dvc_track.sh

# Then commit the resulting tracking files:
git add references.dvc campaigns.dvc .gitignore
git commit -m "chore(dvc): Phase 1.5 first-time bulk snapshot"
git push
```

This is a one-time operational step. It does NOT need to run on every
campaign or every commit — DVC tracking is opt-in for the specific
paths added here.

### Status as of 2026-05-06

- ✅ `references/` snapshotted (empty placeholder; future RAG corpora
  land here and a follow-up `dvc add references` updates the tracking).
- ✅ `campaigns/` snapshotted (4720 files, 26 MB at first snapshot).
  Required prerequisite: the YAML campaign template that previously
  lived at `campaigns/example.yaml` was moved to
  `campaign_templates/example.yaml` so DVC could add `campaigns/`
  without colliding with a git-tracked file inside it.

YAML campaign templates now live under `campaign_templates/` (in git);
per-campaign evidence crates live under `campaigns/` (DVC-tracked, on
TrueNAS). For per-RAG-corpus or per-campaign re-snapshots going forward,
re-run `scripts/dvc_track.sh` as described in the "Tracking the bulky
directories" section above.

## Postgres durable store (Phase 2.1)

The orchestrator dual-writes campaigns, runs, evidence-bundle metadata,
LLM calls, and per-day model stats to Postgres alongside the canonical
JSON files under `memory/`, `runs/`, `campaigns/`. JSON stays canonical
— Postgres is the queryable mirror that enables Phase 2.4 budget
aggregates and Phase 2.6 UI list/filter/sort. Dual-writes are JSON
first, Postgres second; on Postgres failure the run keeps succeeding
(structured WARN log + Prometheus counter increment) and
reconcile-on-startup heals any gap. Toggleable via the
`postgres.enabled` config flag.

### Topology

| Component | Where | Notes |
|---|---|---|
| Postgres server | dedicated LXC (operator's choice — suggested LXC 202) | Debian 12 + postgresql-16 from apt.postgresql.org |
| Database | `orchestrator` | owned by role `orchestrator` |
| Auth | scram-sha-256 password from `POSTGRES_DSN` | role can connect from 192.168.2.0/24 (LAN) and 100.64.0.0/10 (Tailscale CGNAT, in case Tailscale is added later) |
| Daily backup | `/var/backups/postgres/orchestrator-YYYYMMDD-HHMMSS.dump` (cron.daily) | redirect via `BACKUP_DIR=/mnt/nfs/...` env override on the cron — independence from this LXC's filesystem is the whole point |
| Schema migrations | `alembic upgrade head` from the orchestrator LXC | DSN read directly from `.env`, not via `core.config` |

### One-time Postgres setup

1. On Proxmox: create a fresh Debian 12 LXC. Suggested ID 202, 1 vCPU,
   2 GB RAM, 20 GB disk. Bridge `vmbr0`. Static IP on the LAN
   (e.g. `192.168.2.184`).
2. From the Proxmox host, copy the script in and run it:
   ```bash
   pct push 202 /opt/ai-orchestrator/scripts/install_postgres.sh /root/install_postgres.sh
   pct enter 202
   bash /root/install_postgres.sh
   ```
   The script installs `sudo` + `locales-all` + postgresql-16 + contrib,
   generates a URL-safe alphanumeric password (printed at the end —
   save it then), creates the `orchestrator` role + database from
   `template0` with explicit UTF-8 encoding, sets `listen_addresses='*'`,
   scopes `pg_hba.conf` to the LAN + Tailscale CGNAT, and installs the
   daily `pg_dump` cron. **Pre-set** `POSTGRES_ORCHESTRATOR_PASSWORD`
   in the environment if you want to choose the password yourself
   (must be URL-safe — no `/+=@:?#%&` — to interpolate into POSTGRES_DSN
   without encoding pitfalls).
4. From orchestrator LXC 200, smoke-test connectivity:
   ```bash
   apt-get install -y postgresql-client
   PGPASSWORD='<password>' psql -h <postgres-lxc-ip> -U orchestrator \
       -d orchestrator -c 'SELECT version()'
   ```
5. Add `POSTGRES_DSN` to `/opt/ai-orchestrator/.env`:
   ```
   POSTGRES_DSN=postgresql://orchestrator:<password>@<postgres-lxc-ip>:5432/orchestrator
   ```
6. Apply schema migrations from orchestrator LXC 200:
   ```bash
   cd /opt/ai-orchestrator && source venv/bin/activate
   alembic upgrade head
   ```
7. Flip `postgres.enabled = true` in `config.json` (under the new
   `postgres` block) and restart the orchestrator service. On startup,
   reconcile sweeps existing JSON state into the new Postgres rows
   (one-shot) and emits a `reconcile_completed` log line.

### Backup independence

The default cron writes dumps to `/var/backups/postgres/` on the
Postgres LXC's local disk. **This is not "independent" yet** — a disk
failure on that LXC takes both the live database and its backups. The
production setup adds an SSH-based off-site rsync to TrueNAS using a
dedicated keypair, mirroring the Phase 1.4 DVC trust pattern (no NFS
or SMB share needed).

**Setup as ofRunbook 2026-05-07** (LXC 202 → TrueNAS):

1. **Install rsync on the Postgres LXC** (omitted from the bootstrap):
   ```bash
   pct exec 202 -- apt-get install -y rsync
   ```
2. **Generate a dedicated keypair on the Postgres LXC** (separate from
   any other key — easy to revoke):
   ```bash
   pct exec 202 -- ssh-keygen -t ed25519 -N "" \
       -C "postgres-server-pgbackup@$(date -u +%Y-%m-%d)" \
       -f /root/.ssh/id_ed25519_pgbackup
   ```
3. **Append the pubkey to dvc-orchestrator's authorized_keys on
   TrueNAS** (run from the orchestrator LXC, which already has the
   Phase 1.4 DVC key):
   ```bash
   PUBKEY=$(pct exec 202 -- cat /root/.ssh/id_ed25519_pgbackup.pub)
   ssh -i /root/.ssh/id_ed25519_dvc dvc-orchestrator@192.168.2.222 \
       "grep -qF '$PUBKEY' ~/.ssh/authorized_keys || echo '$PUBKEY' >> ~/.ssh/authorized_keys"
   ```
   And create the destination directory:
   ```bash
   ssh -i /root/.ssh/id_ed25519_dvc dvc-orchestrator@192.168.2.222 \
       "mkdir -p ~/pgbackup"
   ```
4. **Prime the Postgres LXC's known_hosts** so the cron's
   `BatchMode=yes` SSH won't bail on first contact:
   ```bash
   pct exec 202 -- bash -c \
       'ssh-keyscan -t ed25519 192.168.2.222 >> /root/.ssh/known_hosts'
   ```
5. **Replace `/etc/cron.daily/orchestrator-pgdump`** on the Postgres
   LXC with the off-site version. Local retention 7 days, TrueNAS
   retention 30 days. Both are configurable via env-var prefixes.
   ```bash
   cat > /tmp/orchestrator-pgdump <<'CRON'
   #!/bin/sh
   set -eu
   LOCAL_DIR="${BACKUP_DIR:-/var/backups/postgres}"
   REMOTE_USER="${PGBACKUP_REMOTE_USER:-dvc-orchestrator}"
   REMOTE_HOST="${PGBACKUP_REMOTE_HOST:-192.168.2.222}"
   REMOTE_DIR="${PGBACKUP_REMOTE_DIR:-/mnt/f3/dvc-orch-home/dvc-orchestrator/pgbackup}"
   SSH_KEY="${PGBACKUP_SSH_KEY:-/root/.ssh/id_ed25519_pgbackup}"
   TS="$(date -u +%Y%m%d-%H%M%S)"
   DUMP="${LOCAL_DIR}/orchestrator-${TS}.dump"
   mkdir -p "$LOCAL_DIR"
   sudo -u postgres pg_dump --format=custom --compress=9 \
       --file="$DUMP" orchestrator
   rsync -a -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes" \
       "$DUMP" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/" \
       || echo "[orchestrator-pgdump] WARN: rsync to TrueNAS failed; local copy at $DUMP" >&2
   find "$LOCAL_DIR" -name 'orchestrator-*.dump' -mtime +7 -delete
   ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
       "${REMOTE_USER}@${REMOTE_HOST}" \
       "find '$REMOTE_DIR' -name 'orchestrator-*.dump' -mtime +30 -delete" \
       || true
   CRON
   pct push 202 /tmp/orchestrator-pgdump /etc/cron.daily/orchestrator-pgdump
   pct exec 202 -- chmod 0755 /etc/cron.daily/orchestrator-pgdump
   ```
6. **Run it once manually** to verify:
   ```bash
   pct exec 202 -- bash /etc/cron.daily/orchestrator-pgdump
   ssh -i /root/.ssh/id_ed25519_dvc dvc-orchestrator@192.168.2.222 \
       'ls -la ~/pgbackup/'
   ```
   You should see a fresh `orchestrator-<timestamp>.dump` of ~100 KB+.

The off-site copy is what survives an LXC-disk failure or a Proxmox
host loss; the local copy is for fast restores. Each side has its own
retention to keep the local LXC slim while keeping a full month of
history off-site.

Restore: `scp` the dump from TrueNAS back to the Postgres LXC, then
`pg_restore --no-owner --dbname=orchestrator <dump>`. See "Restoring
from a pg_dump" below.

### Common Postgres issues

- `psql: error: connection to server ... refused` from orchestrator
  LXC → check `listen_addresses` is `'*'` in `postgresql.conf` and the
  postgresql service is running. The script sets these but a
  pre-existing install might override.
- `FATAL: no pg_hba.conf entry for host ...` → orchestrator LXC's IP
  is outside the `192.168.2.0/24` and Tailscale ranges in `pg_hba.conf`.
  Add an explicit `host orchestrator orchestrator <ip>/32 scram-sha-256`
  line and `systemctl reload postgresql@16-main`.
- `FATAL: password authentication failed for user "orchestrator"` →
  `POSTGRES_DSN` in orchestrator's `.env` doesn't match the password
  passed to `install_postgres.sh`. Re-run the install script with the
  correct password (the script's `ALTER ROLE` updates without dropping
  the database).
- Reconcile never runs at startup → `postgres.enabled` is still `false`
  in `config.json`, or `reconcile_on_startup` is `false`. Both default
  to safe values; flip them once the LXC is up.
- Dual-write WARN log lines `postgres_writethrough_failed table=runs
  ...` → the orchestrator can't reach Postgres. Runs keep succeeding
  (JSON canonical); the missing rows are recovered by reconcile on the
  next orchestrator restart. Investigate networking between LXC 200
  and the Postgres LXC.

### Restoring from a pg_dump

```bash
# On the Postgres LXC, with the orchestrator app stopped:
sudo -u postgres dropdb orchestrator
sudo -u postgres createdb -O orchestrator orchestrator
sudo -u postgres pg_restore --dbname=orchestrator --no-owner \
    /var/backups/postgres/orchestrator-<timestamp>.dump
```

JSON files on the orchestrator LXC remain canonical, so even a total
Postgres-LXC loss is recoverable: rebuild the LXC, re-run
`install_postgres.sh`, run `alembic upgrade head`, restart the
orchestrator service, and reconcile-on-startup re-populates every row
from the JSON state. The `pg_dump` chain is for fast recovery, not
disaster recovery.

## Redis ephemeral store (Phase 2.2)

The orchestrator uses Redis as the cross-process coordination + cache
layer for ephemeral state: live `RUN_STATUS` (Phase 2.2.2), WebSocket
client pub/sub (Phase 2.2.3), and the LLM URL + embedding caches
(Phase 2.2.4). In-process state under `core/runtime` and
`llm/ollama` remains the fast path; Redis is failure-tolerant — when
unreachable, the orchestrator falls back to in-process semantics. No
durable run data lives in Redis: completed runs are canonical in
`memory/run_index.json` and mirrored to Postgres.

Toggleable via the `redis.enabled` config flag.

### Topology

| Component | Where | Notes |
|---|---|---|
| Redis server | dedicated LXC (operator's choice — suggested LXC 203) | Debian 12 + redis-server 7.0.x from base apt |
| Auth | `requirepass` from `REDIS_URL` | LAN-bound, no public exposure |
| Persistence | AOF, `appendfsync everysec` | ≤1s data loss on crash; survives restart |
| Eviction | `allkeys-lru` | cache-friendly once Phase 2.2.4 caches load |
| Backup | none by default | ephemeral by design; see "Backup (optional)" below |

### One-time Redis setup

1. On Proxmox: create a fresh Debian 12 LXC. Suggested ID 203, 1 vCPU,
   2 GB RAM, 10 GB disk. Bridge `vmbr0`. Static IP on the LAN
   (e.g. `192.168.2.185`).
2. From the Proxmox host, copy the script in and run it:
   ```bash
   pct push 203 /opt/ai-orchestrator/scripts/install_redis.sh /root/install_redis.sh
   pct enter 203
   bash /root/install_redis.sh
   ```
   The script installs `redis-server`, generates a URL-safe
   alphanumeric `requirepass` (printed at the end — save it then),
   sets `bind 0.0.0.0 ::`, enables AOF persistence with
   `appendfsync everysec`, and sets `maxmemory-policy allkeys-lru`.
   **Pre-set** `REDIS_ORCHESTRATOR_PASSWORD` in the environment if you
   want to choose the password yourself (must be URL-safe — no
   `/+=@:?#%&`).
3. From orchestrator LXC 200, smoke-test connectivity:
   ```bash
   apt-get install -y redis-tools
   redis-cli -h <redis-lxc-ip> -a '<password>' --no-auth-warning ping
   ```
4. Add `REDIS_URL` to `/opt/ai-orchestrator/.env`:
   ```
   REDIS_URL=redis://:<password>@<redis-lxc-ip>:6379/0
   ```
5. Flip `redis.enabled = true` in `config.json` (under the new `redis`
   block) and restart the orchestrator service. On startup, the
   orchestrator falls back to in-process state if Redis is unreachable,
   so this flip is non-disruptive.
6. Verify with the live-marker test suite:
   ```bash
   cd /opt/ai-orchestrator && source venv/bin/activate
   REDIS_URL='redis://:<password>@<redis-lxc-ip>:6379/0' \
       python -m pytest -m redis_real -q
   ```

### Backup (optional)

Redis holds ephemeral state by design; losing it costs at most a
single in-flight run's progress and a cold cache. If you want backups
anyway:

```sh
# /etc/cron.daily/orchestrator-redis-dump (operator action — not
# installed by install_redis.sh)
#!/bin/sh
set -eu
BACKUP_DIR="${BACKUP_DIR:-/var/backups/redis}"
mkdir -p "$BACKUP_DIR"
TS="$(date -u +%Y%m%d-%H%M%S)"
PASS="$(awk '/^requirepass /{print $2}' /etc/redis/redis.conf)"
redis-cli -a "$PASS" --no-auth-warning BGSAVE >/dev/null
sleep 5
tar -C /var/lib -czf "${BACKUP_DIR}/redis-${TS}.tar.gz" redis
find "${BACKUP_DIR}" -name 'redis-*.tar.gz' -mtime +14 -delete
```

Same off-LXC redirection trick as the Postgres backup applies — point
`BACKUP_DIR` at an SSHFS / NFS mount of TrueNAS for true backup
independence.

### Common Redis issues

- `NOAUTH Authentication required` from `redis-cli` → forgot the `-a
  <password>` flag (or the password mismatches). Re-run with the value
  from `/etc/redis/redis.conf:requirepass`.
- `Could not connect to Redis at 192.168.2.x:6379: Connection refused`
  → either redis-server isn't running (`systemctl status redis-server`)
  or `bind` wasn't widened off `127.0.0.1`. Check the active config
  with `redis-cli CONFIG GET bind` (must include `0.0.0.0` for the
  orchestrator LXC to reach it).
- Orchestrator falls back to in-process state silently → that's the
  designed failure mode. Check the orchestrator's structured WARN logs
  for `redis_*` lines, then `redis-cli -h <lxc-ip> -a <pass> ping` from
  LXC 200 to isolate networking vs. Redis-process problems.

## OpenTelemetry tracing (Phase 2.3)

The orchestrator emits traces via OpenTelemetry/OTLP-gRPC to a
self-hosted Grafana Tempo backend. FastAPI (every HTTP request) and
the `requests` library (every outbound call — Ollama, ntfy, Gotify,
NoteDiscovery) are auto-instrumented; ``log()``, ``ssh_command``, and
the two ``query_ollama*`` LLM entrypoints have manual spans with
domain attributes (``orchestrator.run_id``, ``llm.model``, ``llm.role``,
``ssh.target``, etc.).

Toggleable via the `otel.enabled` config flag. When disabled, every
manual span call is zero-cost — OTel falls back to the no-op default
TracerProvider.

### Topology

| Component | Where | Notes |
|---|---|---|
| Tempo server | dedicated LXC (operator's choice — suggested LXC 204) | Debian 12 + Tempo 2.6.x single-binary from grafana.com release |
| OTLP/gRPC | `<tempo-lxc-ip>:4317` | trace ingest endpoint; orchestrator's `OTEL_ENDPOINT` points here |
| OTLP/HTTP | `<tempo-lxc-ip>:4318` | HTTP/JSON ingest (unused by orchestrator but available for sidecars) |
| Query API | `<tempo-lxc-ip>:3200` | Grafana datasource + ad-hoc curl queries |
| Storage | local-blocks at `/var/lib/tempo/blocks` (block-storage backend) | 14d retention by default; bump in `/etc/tempo/tempo.yaml` |
| Sampling | head-based via `TraceIdRatioBased` | `otel.sample_ratio=1.0` records every trace; lower for high-volume environments |

### One-time Tempo setup

1. On Proxmox: create a fresh Debian 12 LXC. Suggested ID 204, 1 vCPU,
   2 GB RAM, 10 GB disk. Bridge `vmbr0`. Static IP on the LAN
   (e.g. `192.168.2.187`).
2. From the Proxmox host, copy the script in and run it:
   ```bash
   pct push 204 /opt/ai-orchestrator/scripts/install_tempo.sh /root/install_tempo.sh
   pct enter 204
   bash /root/install_tempo.sh
   ```
   The script downloads the Tempo binary tarball from
   `github.com/grafana/tempo/releases`, installs it to
   `/usr/local/bin/tempo`, creates the `tempo` system user + data
   directories, writes `/etc/tempo/tempo.yaml` (single-binary mode,
   OTLP/gRPC :4317, OTLP/HTTP :4318, query :3200, local-blocks backend),
   installs a systemd unit and starts it. **Pre-set** `TEMPO_VERSION`
   to bump from the script default. Health check via `/ready` on :3200
   takes ~5s after first start; the script polls until it returns 200.
3. From orchestrator LXC 200, smoke-test connectivity:
   ```bash
   curl http://<tempo-lxc-ip>:3200/ready    # → "ready"
   nc -z <tempo-lxc-ip> 4317                 # OTLP/gRPC reachable
   ```
4. Add `OTEL_ENDPOINT` to `/opt/ai-orchestrator/.env`:
   ```
   OTEL_ENDPOINT=<tempo-lxc-ip>:4317
   ```
5. Flip `otel.enabled = true` in `config.json` and restart the
   orchestrator service. On startup, ``init_tracing(app)`` builds the
   global TracerProvider, attaches a BatchSpanProcessor, and
   instruments FastAPI + requests.
6. Verify traces flow:
   ```bash
   # Generate some traffic
   for i in 1 2 3 4 5; do curl -fsS http://127.0.0.1:8000/health > /dev/null; done
   # Wait ~5s for the BatchSpanProcessor to flush, then query Tempo
   sleep 6
   curl 'http://<tempo-lxc-ip>:3200/api/search?tags=service.name%3Dai-orchestrator&limit=5'
   ```
   The response will include trace IDs with `rootServiceName=ai-orchestrator`.

### Common Tempo issues

- `/ready` returns 503 right after start → Tempo's ingester takes
  ~10–30s to join the ring on first boot. Wait, then re-check.
- `level=warn ... feature gate ID=component.UseLocalHostAsDefaultHost`
  in journalctl → harmless. Tempo recommends explicit local-host
  binds for production; for a homelab LXC behind a LAN-only firewall,
  `0.0.0.0` is fine.
- Traces present in Tempo but missing manual span attributes (e.g. no
  `orchestrator.run_id`) → ensure the orchestrator restarted AFTER
  `otel.enabled=true` was set; init_tracing only runs at boot.
- Cardinality blowup → drop `otel.sample_ratio` to `0.1` (10%) to
  reduce volume tenfold without losing the ability to investigate.

### Disabling tracing

Set `otel.enabled=false` in `config.json` and restart. Manual span
sites (log, ssh_command, LLM calls) all delegate to OTel's no-op
TracerProvider; the cost is a single attribute read per call. The
Tempo LXC can be shut down without affecting the orchestrator.

## Grafana dashboards (Phase 2.3)

Grafana is the visualization layer for Phase 2.3 traces (via Tempo)
and Phase 1.8.5 metrics (via the orchestrator's own `/metrics`
endpoint). Datasources and dashboards are auto-provisioned by
``scripts/install_grafana.sh`` from YAML / JSON files under
``/etc/grafana/provisioning/`` and ``/var/lib/grafana/dashboards/``.

### Topology

| Component | Where | Notes |
|---|---|---|
| Grafana server | dedicated LXC (operator's choice — suggested LXC 205) | Debian 12 + grafana-oss 12.4.3 from `apt.grafana.com` |
| HTTP UI | `<grafana-lxc-ip>:3000` | basic-auth with admin / `<grafana-admin-password>` |
| Tempo datasource | UID `tempo-orchestrator` | proxies queries to `<tempo-lxc-ip>:3200` |
| Prometheus datasource | UID `prometheus-orchestrator` | proxies to orchestrator's `/metrics` (no separate Prometheus server in Phase 2.3) |
| Per-run trace dashboard | UID `orchestrator-per-run` | TraceQL filter on `orchestrator.run_id`; paste the run_id in the textbox |

### One-time Grafana setup

1. On Proxmox: create a fresh Debian 12 LXC. Suggested ID 205, 1 vCPU,
   2 GB RAM, 8 GB disk. Bridge `vmbr0`. Static IP on the LAN
   (e.g. `192.168.2.188`).
2. From the Proxmox host, copy the script in and run it:
   ```bash
   pct push 205 /opt/ai-orchestrator/scripts/install_grafana.sh /root/install_grafana.sh
   pct enter 205
   bash /root/install_grafana.sh
   ```
   The script installs `grafana=12.4.3` from `apt.grafana.com`,
   generates a URL-safe alphanumeric admin password (printed at the
   end — save it then), writes the password into
   `[security].admin_password` of `grafana.ini`, removes the SQLite
   `grafana.db` so first-boot user creation picks up the new
   password, provisions the Tempo + Prometheus datasources, drops
   the per-run trace lookup dashboard at
   `/var/lib/grafana/dashboards/orchestrator/per-run-traces.json`,
   and starts grafana-server.
   **Pre-set** `GRAFANA_ADMIN_PASSWORD` / `TEMPO_URL` /
   `PROMETHEUS_URL` env vars to override defaults.
   **Why pin to 12.4.3?** Grafana 13.0.1 has a regression where
   `grafana-cli admin reset-admin-password` writes a hash that the
   running server rejects (verified 2026-05-07). 12.4.3 is the
   current LTS; bump `GRAFANA_VERSION` once 13.x is fixed.
3. From any LAN host, browse to `http://<grafana-lxc-ip>:3000` and
   log in as `admin` / `<password>`. The "AI Orchestrator — Per-run
   traces" dashboard appears automatically.
4. To verify datasources via API:
   ```bash
   curl -u admin:<pass> http://<grafana-lxc-ip>:3000/api/datasources
   # Should list Tempo + Prometheus.
   curl -u admin:<pass> \
       'http://<grafana-lxc-ip>:3000/api/datasources/proxy/uid/tempo-orchestrator/api/echo'
   # Should return "echo" — confirms Grafana → Tempo connectivity.
   ```

### Common Grafana issues

- HTTP 401 with `[password-auth.invalid] invalid password` after
  install → the brute-force lockout kicked in OR you're hitting the
  Grafana 13 reset-admin-password regression. Stop the service,
  remove `/var/lib/grafana/grafana.db`, restart — first-boot user
  creation picks up `[security].admin_password` from `grafana.ini`.
- `too many consecutive incorrect login attempts for user — login
  for user temporarily blocked` → 5 failed attempts triggered
  Grafana's brute-force protection. Wait ~5min OR clear via
  `sqlite3 /var/lib/grafana/grafana.db 'DELETE FROM login_attempt;'`
  (admin recovery only — never a normal flow).
- Plugin install error in journalctl
  (`unlinkat /usr/share/grafana/data/plugins-bundled/elasticsearch:
  read-only file system`) → harmless. The bundled-plugin
  auto-update tries to upgrade itself but the bundle dir is
  read-only on packaged installs. Functionality unaffected.
- Empty trace search → confirm `otel.enabled=true` in orchestrator
  config and traffic has flowed since the last orchestrator restart.
  `BatchSpanProcessor` flushes on a 5s interval — wait at least 6s
  after the last call before querying.

### Disabling Grafana

The Grafana LXC is a pure consumer of Tempo + the orchestrator's
`/metrics` — shutting it down has zero impact on the orchestrator.
`systemctl stop grafana-server` on the Grafana LXC, or `pct stop
205` from Proxmox, both work cleanly.

## SkyPilot cloud-burst (Phase 2.5)

For workloads that exceed the homelab GPU budget — large-batch
fine-tunes, multi-GPU evals, peak demand — the orchestrator can
spin up a cloud GPU on demand via [SkyPilot]. Ships **dormant by
default**: even with the SDK installed, no provisioning happens
until you flip `sky.enabled=true` AND configure a cloud provider.

### Topology

| Component | Where | Notes |
|---|---|---|
| SkyPilot SDK | orchestrator LXC 200 (already in `requirements.txt`) | wrapped by `core/sky.py` |
| YAML specs | `sky/*.yaml` (git-tracked) | `llm-burst.yaml` (Ollama on a GPU) + `torch-eval.yaml` (PyTorch one-shot) |
| Provider creds | `~/.runpod/api_key.toml` or `RUNPOD_API_KEY` env (RunPod) / `~/.config/vastai/vast_api_key` (Vast) | stays out of `config.json` |
| Burst route | `POST /runs/{id}/burst` | accepts `spec_name` + optional overrides |
| Idle failsafe | background daemon (Phase 2.5.4) | stops clusters with no activity for `idle_timeout_minutes` |
| Cost ceiling | `sky.max_burst_cost_usd` per burst + Phase 2.4 budget aggregate per campaign | both enforced before launch |

### One-time setup (operator)

1. Pick a provider — RunPod is the typical default for spot GPU.
   Sign up, generate an API key.
2. Drop the key in the location SkyPilot expects:
   ```bash
   # RunPod
   mkdir -p ~/.runpod
   cat > ~/.runpod/api_key.toml <<'EOF'
   api_key = "rpa_..."
   EOF
   chmod 600 ~/.runpod/api_key.toml
   # Vast.ai
   mkdir -p ~/.config/vastai
   echo 'YOUR_API_KEY' > ~/.config/vastai/vast_api_key
   chmod 600 ~/.config/vastai/vast_api_key
   ```
3. Verify SkyPilot recognises the credentials:
   ```bash
   cd /opt/ai-orchestrator && source venv/bin/activate
   sky check
   # Expected: at least one cloud reports "✓ enabled"
   ```
4. Flip `sky.enabled = true` in `config.json` and restart the
   orchestrator.
5. Test-launch the smallest spec to confirm wiring:
   ```bash
   curl -X POST http://127.0.0.1:8000/runs/<run-id>/burst \
       -H 'Content-Type: application/json' \
       -d '{"spec_name": "llm-burst", "estimated_cost_usd": 0.50}'
   ```

### Built-in YAML specs

- **`sky/llm-burst.yaml`** — provisions a single GPU, installs Ollama,
  pulls `${OLLAMA_MODEL}` (override via env), serves on :11434. The
  consumer's own task connects to the SkyPilot-exposed endpoint and
  drives LLM calls. Idle-stop applies.
- **`sky/torch-eval.yaml`** — one-shot PyTorch eval. Installs
  torch+torchvision+torchaudio, downloads the `EVAL_SCRIPT_URL`,
  runs it, exits. SkyPilot tears the cluster down on completion (no
  idle window).

Add new specs to `sky/` and they're available to the burst route by
basename. Specs follow the standard SkyPilot YAML schema; see
[docs.skypilot.co/en/latest/reference/yaml-spec.html](https://docs.skypilot.co/en/latest/reference/yaml-spec.html).

### Cost discipline

Two ceilings stack to bound risk:

1. **Per-burst** — `sky.max_burst_cost_usd` rejects launches whose
   ``estimated_cost_usd`` exceeds the ceiling. The route caller
   computes the estimate from accelerator + expected wall-clock.
2. **Per-campaign** — Phase 2.4 budget tracking accrues the burst's
   actual cost (via `sky cost-report`) into
   `campaigns.budget_used_usd` once the cluster terminates. If the
   campaign breaches 100%, the burst's parent run gets paused along
   with the campaign.

The idle-stop daemon (Phase 2.5.4) is the third safety net: any
cluster that goes `idle_timeout_minutes` without activity gets
`sky stop`-ed automatically.

### Common SkyPilot issues

- `sky check` reports no clouds enabled → API key file is in the
  wrong location or wrong format. Re-read the provider's section in
  the SkyPilot docs.
- Burst stuck in `INIT` for >10 min → provider has no spot capacity
  for the requested accelerator. Switch to on-demand
  (`use_spot: false` in the YAML — already the default) or pick a
  different accelerator.
- Cluster appears in `sky status` but the orchestrator's `list_active_
  bursts` is empty → ran `sky launch` directly outside the
  orchestrator. The idle-stop daemon only manages clusters whose
  names match orchestrator-owned prefixes; manual clusters are
  invisible.

### Disabling cloud-burst

Flip `sky.enabled = false` in `config.json` and restart. Already-
running clusters keep running (we don't auto-tear-down on disable —
operators manually `sky down` them). Future launch attempts return
503.

## NoteDiscovery-grounded planner (Phase 3.3)

Before proposing a campaign, the planner queries NoteDiscovery
(Docker LXC at `192.168.2.203:8010` by default) for vault notes
relevant to the prompt and prepends them to its memory context.
The full query→results trace is persisted at
`memory/<run_id>/planner_research.json`; the Phase 1.2 evidence
bundle picks them up as `references` and emits them as RO-Crate
`citation` entities.

### Topology

- **NoteDiscovery server**: 192.168.2.203:8010 (your existing
  NoteDiscovery container; no MCP shim needed — the orchestrator
  talks REST).
- **Endpoints used**: `GET /api/search?q=…&limit=N` and
  `GET /health`. Auth declared in OpenAPI but not enforced as of
  NoteDiscovery 0.19.1; the orchestrator sends an optional
  `X-API-Key` header from `.env` (`NOTEDISCOVERY_API_KEY`) for
  forward-compat.

### One-time setup (operator)

1. Ensure NoteDiscovery is reachable: `curl
   http://192.168.2.203:8010/health` returns
   `{"status":"healthy",...}`.
2. (Optional) drop `NOTEDISCOVERY_API_KEY=…` into
   `/opt/ai-orchestrator/.env` if NoteDiscovery is later tightened
   to enforce auth.
3. Edit live `config.json`:

   ```json
   "note_discovery": {
     "enabled": true,
     "base_url": "http://192.168.2.203:8010",
     "top_k": 8,
     "timeout_seconds": 30
   }
   ```

4. `systemctl restart ai-orchestrator` and watch for
   `note_discovery: reachable` in the journal. If you see
   `WARNING: NoteDiscovery enabled but healthcheck failed`, the
   container isn't reachable and the planner will continue without
   research grounding (fail-open).

5. Trigger a campaign and inspect:
   - `memory/<run_id>/planner_research.json` — query + result trace
   - Evidence crate `evidence.json::references` and the RO-Crate
     `citation` array under the root Dataset
   - `/metrics | grep notediscovery` — counter + duration histogram

### Common NoteDiscovery issues

- Empty `results` despite a populated vault → query phrase doesn't
  match. NoteDiscovery does substring search; rephrase or raise
  `top_k`.
- Healthcheck shows `unreachable` after a NoteDiscovery upgrade →
  port may have shifted. Confirm with `curl
  http://192.168.2.203:8010/health` and update `base_url`.
- Snippets contain `<mark class="search-highlight">…</mark>` HTML
  → the `_strip_marks` filter regressed. Open
  `core/note_discovery.py:_HIGHLIGHT_RE` and adjust the regex.

### Disabling NoteDiscovery

Flip `note_discovery.enabled = false` in `config.json` and restart.
The planner falls back to its existing memory stack on every search
call; existing `planner_research.json` traces remain on disk.
