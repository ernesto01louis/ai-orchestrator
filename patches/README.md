# patches/

One-off, **operator-run** setup/patch scripts. They are **not** imported by the
orchestrator and **not** run automatically. Each is applied by hand on the
deployment host once, is **idempotent** (safe to re-run — guarded by a marker
or a presence check), and **backs up** the file it edits before changing it.

| Script | What it does | Target (backup) | Re-runnable? |
|---|---|---|---|
| `add_mcp_servers_slot.py` | Injects an `mcp_servers` block (blender / playwright / ffmpeg entries) into the live config. | `/opt/ai-orchestrator/config.json` (`.json.bak.mcp`) | Yes — only adds missing keys. |
| `kazuki_avatar_step8.py` | Patches the legacy Thanatos UI to add the WebSocket-driven "Kazuki" sprite avatar (CSS/HTML/JS, anchored string injection). | `/opt/ai-orchestrator/ui/graph.html` (`.html.bak.step8`) | Yes — marker-guarded; no-ops if applied. |

## Usage

Run on the orchestrator host after a deploy, e.g.:

```bash
/opt/ai-orchestrator/venv/bin/python patches/add_mcp_servers_slot.py
```

## Notes

- Paths are hardcoded to the standard `/opt/ai-orchestrator` install location.
- These mutate **runtime files in place** (not the repo), so they live here
  rather than in `scripts/` (which holds Proxmox provisioning/bootstrap). Treat
  them as post-install tweaks. Add new one-shot patches here and append a row.
