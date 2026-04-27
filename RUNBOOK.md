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
   {"name": "newhost", "host": "192.168.2.X", "username": "louis", "key_path": "/root/.ssh/id_rsa"}
   ```
2. Make sure the key is authorized on the new host:
   ```bash
   ssh-copy-id -i /root/.ssh/id_rsa.pub louis@192.168.2.X
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
