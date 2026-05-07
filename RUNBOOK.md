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

Failed WebSocket auth is rejected with close code `4401` before the
handshake completes (per Phase 1.7 design).

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
