# RESTORE — disaster recovery for the AI Orchestrator

Authoritative procedure for backing up the orchestrator LXC and restoring it
on bare metal. RUNBOOK.md has the day-to-day op steps; this doc is for the
"the LXC is gone, where do I get the data back from?" situations.

> **Status as of 2026-05-06:** rsync target on TrueNAS exists (`Backups/`
> directory on the existing CIFS mount); the DVC remote at
> `/mnt/f3/orchestrator-dvc` carries citation-grade evidence-bundle data.
> What's NOT yet automated: the periodic rsync cron (script lands here, but
> the user toggles activation), an offsite copy, and the quarterly restore
> drill. Action items at the bottom.

---

## What's at risk vs. what's already redundant

The orchestrator's state lives in three buckets, in increasing order of
recovery difficulty:

| Bucket | Where | Already redundant? | Recovery channel |
|---|---|---|---|
| Source code | `/opt/ai-orchestrator/` | Yes — git remote on GitHub | `git clone` |
| DVC-tracked binary artifacts | `references/`, `campaigns/<id>/` (gitignored) | Yes — `.dvc` tracking files committed to git, blobs live on TrueNAS at `/mnt/f3/orchestrator-dvc/` | `dvc pull` (Phase 1.4) |
| **Stateful runtime data** | `memory/`, `vault/`, `projects/`, `logs/`, `config.json`, `.env`, `gates.json`, `gates_log.json` | **No — single host, no replica yet** | rsync from TrueNAS backup (this doc) |

The third bucket is the one that needs this RESTORE doc. The other two have
their own recovery channels (git, dvc) that are tested every time someone
clones the repo.

---

## What gets backed up

Run `scripts/backup.sh` (rsyncs to `/mnt/nas-vault/Backups/ai-orchestrator/`)
to capture the stateful bucket. The backup includes:

- `memory/` — per-target identity, primer, session log, model stats,
  positive/negative recall, embedding cache, lessons, targets, vault links
- `vault/` — Obsidian L5 memory (per-run / project / model / target / error / daily notes)
- `projects/<name>/runs/<run_id>/` — per-run artifact tree consumed by the evidence builder
- `config.json` and `.env` — host-local config + secrets (yes, the secret backup
  is intentional; the alternative is needing to remember the Gotify token by hand
  on restore)
- `gates.json` and `gates_log.json` — learned safety rules
- `tool_registry.json` — tool definitions

What's intentionally **excluded**:

- `references/` and `campaigns/<id>/` — DVC carries these to TrueNAS already
- `venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/` — recreated by
  `pip install -r requirements.txt`
- `.git/` — clone from GitHub instead
- `logs/` — informational, not state; let it regenerate

---

## Backup procedure

The script `scripts/backup.sh` is idempotent — re-run any time. Wire it to
cron (or systemd timer) for hands-off operation. Recommended frequency:
nightly + on-demand before risky ops.

```bash
# One-shot
sudo /opt/ai-orchestrator/scripts/backup.sh

# Scheduled (root crontab) — 03:15 UTC nightly
15 3 * * *  /opt/ai-orchestrator/scripts/backup.sh >>/var/log/orchestrator-backup.log 2>&1
```

The backup uses rsync-style timestamps; old snapshots accumulate under
`Backups/ai-orchestrator/snapshots/<UTC-timestamp>/`. Prune by hand
quarterly (or wire `find ... -mtime +N -delete`).

---

## Restore procedure

### Scenario A — service crashed / corruption suspected, no LXC loss

1. **Stop the service** so you don't write into a half-restored tree.
   ```bash
   systemctl stop ai-orchestrator
   ```

2. **Pull latest source** to make sure code matches the data version you're
   about to restore.
   ```bash
   cd /opt/ai-orchestrator
   git pull --ff-only
   pip install -r requirements.txt   # in venv
   ```

3. **Pick a snapshot** — usually the most recent one before the bad event.
   ```bash
   ls -1t /mnt/nas-vault/Backups/ai-orchestrator/snapshots/ | head
   SNAP=/mnt/nas-vault/Backups/ai-orchestrator/snapshots/<chosen-timestamp>
   ```

4. **Sync state back** (in place; don't shell out the orchestrator dir).
   ```bash
   rsync -av --delete "$SNAP/memory/"   /opt/ai-orchestrator/memory/
   rsync -av --delete "$SNAP/vault/"    /opt/ai-orchestrator/vault/
   rsync -av --delete "$SNAP/projects/" /opt/ai-orchestrator/projects/
   cp "$SNAP/config.json" /opt/ai-orchestrator/config.json
   cp "$SNAP/.env"        /opt/ai-orchestrator/.env
   cp "$SNAP/gates.json" "$SNAP/gates_log.json" "$SNAP/tool_registry.json" \
      /opt/ai-orchestrator/
   ```

5. **Pull DVC-tracked binary artifacts** if the campaigns/references trees
   are gone or stale. Skip if they look intact.
   ```bash
   cd /opt/ai-orchestrator
   source venv/bin/activate
   dvc pull
   ```

6. **Restart and verify**.
   ```bash
   systemctl start ai-orchestrator
   curl -s http://127.0.0.1:8000/health | python3 -m json.tool
   ```

   Expected: orchestrator green, Ollama main + judge green (if reachable),
   Hindsight green.

### Scenario B — full LXC loss / rebuild from scratch

1. **Provision a fresh Debian 12 LXC** on Proxmox with the same network
   config (LAN IP 192.168.2.218, hostname `ai-orchestrator`). Mirror the
   network so SSH targets keep authorizing it.

2. **Re-attach the existing TrueNAS CIFS mount** so backups are reachable.
   Copy `/etc/fstab` line from the old LXC:
   ```
   //192.168.2.222/f3 /mnt/nas-vault cifs credentials=/root/.smbcreds,vers=3.0,iocharset=utf8 0 0
   ```
   And `/root/.smbcreds` (you'll need a way to get this — keep a
   sealed-envelope copy somewhere).

3. **Clone the repo + install deps**.
   ```bash
   cd /opt
   git clone https://github.com/ernesto01louis/ai-orchestrator.git
   cd ai-orchestrator
   python3.11 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Generate / copy the SSH key** that was used to push to TrueNAS (DVC
   user) and to the orchestrator's SSH targets. If you have a backup of
   `/root/.ssh/`, restore it. Otherwise re-provision keys per host.

5. **Run the restore steps from Scenario A starting at step 3.**

6. **Install + enable the systemd unit**.
   ```bash
   cp /opt/ai-orchestrator/scripts/ai-orchestrator.service \
       /etc/systemd/system/   # or hand-write per RUNBOOK
   systemctl daemon-reload
   systemctl enable --now ai-orchestrator
   ```

7. **Smoke** — POST a tiny `/orchestrate` from the same node, watch
   `journalctl -u ai-orchestrator -f` for the lifecycle, confirm a run
   completes end-to-end.

---

## Quarterly restore drill (recommended)

Once a quarter, restore into a throwaway LXC and confirm the orchestrator
boots from backup alone. Procedure:

1. Create LXC 299 on Proxmox (same Debian 12 base) — temporary.
2. Mount the same CIFS share read-only so the drill can't accidentally
   modify production backups.
3. Run Scenario B end-to-end against LXC 299.
4. POST a smoke run and verify it completes; check `/health`.
5. Destroy LXC 299.

Log the drill date + outcome in `RESTORE-DRILLS.log` (or a vault note).
A failed drill is a Sev-1 — fix backups before the next production
incident catches you.

---

## Open items (not yet wired)

These are still TODO at the time of writing; tracked here so future work
doesn't duplicate the discovery:

- [ ] **Activate the nightly backup cron.** The `scripts/backup.sh` exists
      but no cron entry is installed yet. Toggle when you're ready to
      accept the noise of nightly logs.
- [ ] **Offsite copy.** Currently everything sits on the same TrueNAS
      array as the live orchestrator's CIFS share. A site-loss event
      (fire, theft, ransomware that climbs through SMB) takes both. Pick
      one of: (a) Proxmox Backup Server on a second box, (b) a small
      cloud bucket with rclone + age-encrypted snapshots, (c) physical
      offsite via rotating USB drives.
- [ ] **First quarterly drill.** Schedule the first one ~90 days after
      this doc lands; log results in `RESTORE-DRILLS.log`.
