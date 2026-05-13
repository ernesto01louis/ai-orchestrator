# SOPS / age secrets at rest

> Phase 0 deferred → PR 3 of the audit-response hardening pass. Closes
> the gap flagged by [SECURITY.md T3](../SECURITY.md#t3--secret-leakage-from-env).

## What this fixes

Plain ``.env`` is gitignored + chmod 600, but the file is still in
plain text on the orchestrator LXC's disk. Anything that can read the
filesystem off-host leaks every secret:

- ``pct backup`` archives the unencrypted ``.env``.
- A compromised apt postinst script (T2 — sudo allowlist blast radius)
  has root and reads ``/opt/ai-orchestrator/.env`` trivially.
- A misplaced ``cp .env /tmp/`` exposes secrets to any other process
  on the same box.

With SOPS + age, only the encrypted ``.env.sops`` lives on disk. The
plaintext only exists in tmpfs (``/run/ai-orchestrator/.env``) for the
lifetime of the service. Backups capture the encrypted blob.

## Components

| File / path | Purpose |
|---|---|
| ``/etc/ai-orchestrator/age/key.txt`` | Operator's age private key. Root-owned, chmod 600, **never committed**. |
| ``.sops.yaml`` (in repo) | Encryption rules — names the age public key(s) authorised to encrypt. |
| ``.env.sops`` (in repo) | Encrypted secrets. Committable. |
| ``scripts/install_sops.sh`` | One-shot bootstrap: install age + sops, generate the keypair, print the public key. |
| ``scripts/decrypt_env.sh`` | Runs at service start (``ExecStartPre``). Decrypts ``.env.sops`` → ``/run/ai-orchestrator/.env``. |
| systemd unit | Calls ``decrypt_env.sh`` before launching uvicorn; the decrypted ``.env`` is sourced via ``EnvironmentFile=``. |

## First-time setup (per orchestrator LXC)

```bash
# 1. Install age + sops, generate the keypair.
sudo bash scripts/install_sops.sh

# Output prints the age public key. Copy it.

# 2. Paste the public key into .sops.yaml:
#    creation_rules[0].age must equal the printed string.
$EDITOR .sops.yaml

# 3. First-time encrypt of the live .env:
sops --encrypt .env > .env.sops

# 4. Verify decrypt:
SOPS_AGE_KEY_FILE=/etc/ai-orchestrator/age/key.txt \
  sops --decrypt .env.sops | head

# 5. Stash the plaintext somewhere safe (or wipe it — you have the
#    decrypted version in /run/ai-orchestrator/.env at runtime).

# 6. Update the systemd unit:
sudo systemctl edit ai-orchestrator.service
# Add:
#   [Service]
#   RuntimeDirectory=ai-orchestrator
#   ExecStartPre=/opt/ai-orchestrator/scripts/decrypt_env.sh
#   EnvironmentFile=-/run/ai-orchestrator/.env

# 7. Reload + restart:
sudo systemctl daemon-reload
sudo systemctl restart ai-orchestrator.service
sudo journalctl -u ai-orchestrator.service -n 20 | grep decrypt_env

# 8. Commit:
git add .env.sops .sops.yaml
git commit -m "feat(secrets): encrypted .env via SOPS + age"
```

## Editing secrets

```bash
# In-place edit — sops decrypts to a tempfile, opens $EDITOR, re-encrypts.
SOPS_AGE_KEY_FILE=/etc/ai-orchestrator/age/key.txt sops .env.sops
```

The diff against the previous ``.env.sops`` is structurally readable
(per-line encryption) so reviewers can see *which* keys changed,
just not what they changed to.

## Adding a second operator

Each operator generates their own keypair on their own host:

```bash
age-keygen -o ~/.config/age/key.txt
# prints the public key
```

Add the new public key to ``.sops.yaml``:

```yaml
creation_rules:
  - path_regex: '^\.env\.sops$'
    age: >-
      age1xxxxxx-orchestrator-lxc-pubkey,
      age1yyyyyy-second-operator-pubkey
```

Then re-encrypt the existing file with the new recipient list:

```bash
sops updatekeys .env.sops
```

The second operator can now decrypt with their own private key. The
file stays the same on disk; the encrypted recipient blocks are
extended.

## Rotation

The orchestrator's age key should be rotated annually or after any
suspected compromise:

```bash
# 1. Generate a fresh key alongside the existing one.
sudo age-keygen -o /etc/ai-orchestrator/age/key-new.txt

# 2. Add the new public key to .sops.yaml (keep the old one for now).

# 3. Re-encrypt with both keys.
sops updatekeys .env.sops

# 4. Verify decrypt works with the new key alone.
SOPS_AGE_KEY_FILE=/etc/ai-orchestrator/age/key-new.txt \
  sops --decrypt .env.sops | head

# 5. Remove the old public key from .sops.yaml.
# 6. sops updatekeys .env.sops  (now only the new key decrypts).
# 7. mv /etc/ai-orchestrator/age/key{-new,}.txt
```

## Recovery (lost age key)

If ``/etc/ai-orchestrator/age/key.txt`` is lost without backup, ``.env.sops``
becomes unreadable. Mitigations:

1. **Backup the key off-host.** Print it onto paper, store in a
   sealed envelope. The key fits on one line.
2. **Multi-recipient setup.** If a second operator's key is also
   authorised, they can re-encrypt for the orchestrator after rotating
   in a fresh key.
3. **Last resort.** Re-generate every secret. Slow but always works.

## When to bypass

For local development against a non-production orchestrator, skip
SOPS entirely: just create a plain ``.env`` (gitignored). The systemd
hook is a no-op when ``.env.sops`` is absent.

## Backward compatibility

The systemd ``ExecStartPre=`` hook is no-op-safe:

- ``.env.sops`` missing → assumes legacy plaintext ``.env`` is in
  place; doesn't touch anything.
- ``sops`` not installed → logs a warning and skips decrypt.
- Decrypt fails → exits non-zero so systemd surfaces the bug.

This means operators on the upgrade path can install SOPS + ship the
new ``.env.sops`` at their own pace; no downtime required.
