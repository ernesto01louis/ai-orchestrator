# Security

## Reporting a vulnerability

Two channels, either is fine:

1. **GitHub Security Advisories** (preferred) — open a private
   advisory at https://github.com/ernesto01louis/ai-orchestrator/security/advisories/new
2. **Email** the maintainer at `louis_ernesto@aol.com` with subject
   line starting `[ai-orchestrator security]`.

Do not file public issues for security problems. We aim to acknowledge
within 7 days and triage within 14.

## Scope

In scope:
- The orchestrator codebase in this repo (`app.py`, `core/`, `llm/`,
  `execution/`, `orchestration/`, `api/`, `evidence/`, `memory_pkg/`,
  `tools/`, `gates.py`, `mcp_server.py`, `cli/`).
- The published Python SDK (`ai-orchestrator-client` on PyPI, source
  at `/opt/ai-orchestrator-client/`).
- Example consumer at [`examples/example-consumer/`](examples/example-consumer/).
- The Proxmox bootstrap scripts under [`scripts/`](scripts/).

Out of scope:
- Third-party services consumed but not maintained here (Ollama,
  Prefect, Postgres, Redis, Tempo, Grafana, DVC, NoteDiscovery,
  TrueNAS, Gotify, ntfy).
- Operator-owned consumer projects that import the SDK.
- The legacy Thanatos UI at [`ui/graph.html`](ui/graph.html) (not
  exposed beyond the local LAN).

## Trust boundaries

```
                         ┌─────────────────────────┐
                         │      Operator (you)     │
                         └─────────────┬───────────┘
                                       │  age key (PR 3) / SSH key
                                       │  bearer token / web UI
                                       ▼
       ┌──────────────────── orchestrator LXC 200 ────────────────────┐
       │ FastAPI app · 5-layer memory · Gates · MCP server            │
       │ Secrets: .env (chmod 600, gitignored, future: SOPS at rest)  │
       └──┬───────────┬────────────┬───────────┬───────────┬──────────┘
          │ SSH       │ HTTP       │ Postgres  │ Redis     │ OTLP/gRPC
          │ (key auth)│ (LAN)      │ DSN+pw    │ requirepass│ (no auth)
          ▼           ▼            ▼           ▼           ▼
     SSH targets  Ollama LXCs  LXC 202     LXC 203     LXC 204 + 205
     (pi-1, pi-2, (216, 219)   Postgres    Redis       Tempo + Grafana
      Rak, ...)
```

Identified trust assumptions:
- **Operator → orchestrator**: bearer token (Phase 1.7) gates REST +
  `/mcp` + `/ws`. The token lives in `.env` and rotates with the
  service.
- **Orchestrator → SSH targets**: per-target `ed25519` keys with
  `StrictHostKeyChecking=accept-new` + shlex-quoted commands. Every
  target shares the same sudo allowlist (see below).
- **Orchestrator → Ollama**: plain HTTP on the LAN. Assumes a trusted
  LAN segment. **Do not expose Ollama LXCs externally without TLS.**
- **Orchestrator → Postgres / Redis**: TCP with password auth on the
  LAN. Same assumption.
- **Orchestrator → Tempo / Prometheus**: no auth, LAN only.

The orchestrator is *not* designed to live on a hostile LAN. The
homelab topology (Proxmox LXCs behind a router NAT) is the implicit
boundary.

## Threat model

### T1 — LLM-generated code is hostile

The orchestrator executes code produced by an LLM agent on SSH
targets. A planner that has been prompt-injected (e.g. via a poisoned
document loaded into a campaign's `references/`) could attempt to
exfiltrate data, install persistence, or pivot.

**Mitigations:**

1. **Hardcoded blocklist** — `tools._TOOL_CMD_BLOCKLIST` rejects
   `rm -rf /`, `mkfs`, fork bombs, pipe-to-shell, common reverse-shell
   patterns. **Cannot be overridden**; no learning path. Note this is a
   pattern-based defense-in-depth heuristic, **not an exhaustive filter** —
   obfuscation (shell quoting, base64, `$()`, command chaining) can evade
   the regexes. The real boundary is the SSH user's own permissions plus
   the sudo allowlist (T2), not this list.
2. **Gates (learned safety)** — [gates.py](gates.py) auto-promotes
   repeated failure patterns in two stages: a **warn** gate after
   `AUTO_PROMOTE_THRESHOLD` occurrences (default 3 — logged but allowed),
   then **auto-escalation of that gate to a hard block** after
   `AUTO_BLOCK_THRESHOLD` occurrences (default 6). Operators can also
   escalate manually at `/gates*`. Phase 3.1 routes gate denials through
   HITL when `hitl_mode != "full_auto"` so the operator gets a
   notification with approve / reject buttons before the gate fires (in
   non-`full_auto` campaigns).
3. **Verify-then-deploy** — generated code runs in a sandbox first
   (`execution/__init__.py:sandbox_execute`) before being deployed
   to a real SSH target. "Sandbox" here means an **earlier-stage SSH
   execution against a separate working directory — not OS-level
   isolation** (no container, namespace, or chroot). Code is constrained
   only by the SSH user's permissions and the sudo allowlist; treat it as
   a staging gate, not a containment boundary.
4. **Path safety** — file path inputs are validated by `SAFE_FILENAME`
   (no `..`, no `/`) plus a `Path.resolve()` containment check in
   every vault / artifact writer.

### T2 — Sudo allowlist blast radius

The `config.json:sudo.allowed_commands` whitelist is the largest
single trust delegation. Defaults include:

```json
[
  "apt-get install -y", "apt-get update", "apt-get remove",
  "npm install -g", "pip install",
  "systemctl start", "systemctl stop", "systemctl restart",
  "systemctl enable", "systemctl status"
]
```

**Blast radius if compromised:**

- `apt-get install -y <pkg>` — arbitrary package install. An attacker
  with planner access could install a package containing a postinst
  script that runs arbitrary code as root.
- `npm install -g <pkg>` — same threat, npm ecosystem.
- `pip install <pkg>` — same threat, PyPI ecosystem.
- `systemctl start/restart` — can launch any installed unit file
  (combined with the apt-get vector, an attacker installs a
  malicious service then starts it).
- `systemctl stop` — can DoS the orchestrator itself or any other
  service on the target host.

**Inherited threat:** every consumer SSH target receives the same
allowlist via `core/config.py:SUDO_ALLOWED_COMMANDS`. There is no
per-target subset today.

**Mitigations in place:**

1. Sudo is **opt-in per deploy** — `config.json:sudo.enabled=false`
   skips the whole layer.
2. Gates can blocklist any individual command pattern.
3. The HITL `gate_only` mode (Phase 3.1) makes every sudo invocation
   visible to the operator if it ever trips a gate.
4. Bearer-token auth (Phase 1.7) prevents an external attacker from
   posting a `/orchestrate` request that would chain into the sudo
   path.

**Recommended operator policies:**

- Run the orchestrator with `sudo.enabled=false` on multi-tenant SSH
  targets (e.g. a shared Raspberry Pi). Restrict sudo to dedicated
  per-orchestrator hosts.
- If a campaign template requests a `pip install` or `apt-get install`,
  add a Gate scoped to that exact package list rather than
  whitelisting the package manager wholesale.
- Audit the allowlist quarterly (ROADMAP "ongoing operational
  hygiene" cadence).

### T3 — Secret leakage from `.env`

Current state: `.env` is gitignored, chmod 600, root-owned on the
orchestrator LXC. Risks:

- LXC backups via `pct backup` capture the unencrypted `.env`.
- Mistaken `cp .env /tmp/` exposes secrets to any other process on
  the box.
- Filesystem-level read by a privileged process (e.g. a compromised
  apt postinst script — see T2).

**Planned mitigation (Phase 0 deferred → PR 3):** SOPS-encrypted
`.env.sops` committed to the repo; the systemd unit decrypts to
`/run/ai-orchestrator/.env` (memfs) at start. Backups capture only
the encrypted blob.

### T4 — MCP client impersonation

Phase 1.7 introduced bearer-token auth gating `/mcp` + REST + `/ws`.
Six paths are intentionally unauthenticated for ops:
`/health`, `/metrics`, `/openapi.json`, `/docs`, `/docs/oauth2-redirect`,
`/redoc` (defined in `core/auth.DEFAULT_PUBLIC_PATHS`).

`/metrics` is auth-bypassed so Prometheus can scrape it; it deliberately
exposes no `run_id` labels (cardinality discipline that also
incidentally limits leakage of project names).

The bearer token lives in `.env` as `ORCHESTRATOR_API_TOKEN`. Rotate
by re-generating, updating `.env`, and restarting the service. All
clients (including the Python SDK via `BearerTokenAuth`) re-read on
reconnect.

### T5 — Evidence bundle tampering

Phase 1.2 signs every evidence bundle with **Ed25519 via PyNaCl**.
The DSSE envelope at `campaigns/<id>/manifest.json.dsse` covers a
SHA256 manifest of every emitted file. Phase 1.5 layers a per-run
SHA256 manifest + per-campaign Merkle root on top.

**Trust root:** a single host-wide signing key at
`/etc/ai-orchestrator/signing/` (generated by
[`scripts/install_signing_key.sh`](scripts/install_signing_key.sh)).
The public key is embedded in every bundle so external verifiers
need nothing beyond the bundle itself.

**Out-of-scope (Phase 4+ backlog):** transparency-log inclusion via
self-hosted Sigstore (Fulcio + Rekor). The DSSE envelope abstracts
the trust root so bundles can later prove inclusion without the
bundle format changing.

### T6 — Path traversal in user inputs

Every file-path field accepted from a client passes through
`SAFE_FILENAME = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]+$")` plus a
`Path.resolve()` containment check. Phase 0.e tightened this against
the prior regex that permitted `..` mid-string.

Known limitation: project names are not pre-validated client-side, so
a malformed `project_name` surfaces as an opaque 422 from the server.
Cosmetic.

## Defense layers (summary)

| Layer | What it does | Where |
|---|---|---|
| 1. Hardcoded blocklist | Rejects known-malicious patterns (defense-in-depth heuristic, not exhaustive) | `tools._TOOL_CMD_BLOCKLIST` |
| 2. Gates (learned) | Auto-promotes repeated failures to a warn gate (N=3), then auto-escalates it to block (N=6) | [gates.py](gates.py) |
| 3. HITL routing | Gate denials → operator approve/reject | `core/hitl.py` (Phase 3.1) |
| 4. Sandbox-first | Staging SSH run (separate dir) before the real target — not OS isolation | `execution/__init__.py` |
| 5. Path safety | SAFE_FILENAME + Path.resolve containment | `core/paths.py` |
| 6. Bearer-token auth | Gates REST + MCP + WS | `core/auth.py` (Phase 1.7) |
| 7. DSSE signing | Tamper-evident evidence bundles | `evidence/signing.py` (Phase 1.2) |
| 8. SHA256 + Merkle | Per-run + per-campaign integrity | `manifest/__init__.py` (Phase 1.5) |
| 9. SOPS (future, PR 3) | Encrypted secrets at rest | `.env.sops` + systemd hook |

## WebSocket cross-thread safety

Phase 0.e fixed `_ws_broadcast` to post coroutines onto the captured
main event loop via `asyncio.run_coroutine_threadsafe`, with a 2s
timeout per send. Background threads (the run thread spawned by
`/orchestrate`, Redis pub/sub subscriber) broadcast safely.

## Dependencies

- `requirements.txt` is pinned. Dependabot scans weekly
  (`.github/dependabot.yml`).
- `pip-audit`, `bandit`, and `gitleaks` run on every push / PR via
  [`.github/workflows/security.yml`](.github/workflows/security.yml)
  (advisory for now). Rotate any flagged transitive deps via a
  `chore(deps-py)` PR.
- `requirements-cloud.txt`, `requirements-eval.txt`, and
  `requirements-plugins.txt` are optional extras — installed only by
  operators who flip the relevant `enabled=true` flag.

## CI verification

Every push triggers `.github/workflows/ci.yml`: ruff + mypy + pytest +
(after PR 2) coverage with a 70% gate (target 80% — ROADMAP success
criterion). A companion `.github/workflows/security.yml` runs
pip-audit + bandit + gitleaks (advisory). Branch protection on `main`
(Phase 0 deferred, operator-action) requires CI to be green before merge.

## What's *not* a security boundary

The following are **not** treated as security boundaries today:

- The LAN between the orchestrator LXC and its dependencies. We assume
  a trusted home network with NAT egress only.
- The Proxmox host (192.168.2.13) — root access there is equivalent
  to root on every LXC.
- TrueNAS at 192.168.2.222 — the DVC remote and Postgres off-site
  backup target share the host's filesystem. Anyone with TrueNAS root
  can read every backup.
- LLM-server prompts. Anyone with read access to `/api/chat` requests
  on the Ollama LXCs sees every system prompt and user prompt the
  orchestrator sends.

If the threat model changes (e.g. moving to a multi-tenant or
public-internet deployment), revisit. The defenses above are
calibrated for a solo-operator homelab.
