# Security

## Reporting a vulnerability

Open a private GitHub security advisory or email the maintainer
directly. Do not file public issues for security problems.

## Secret handling

- Secrets live in `.env` (gitignored, chmod 600), loaded at startup
  via [python-dotenv](https://pypi.org/project/python-dotenv/).
- `config.json` carries structure and non-sensitive settings only;
  `config.example.json` is the safe template.
- Phase 0.b rotated the Gotify token out of `config.json` and into
  `.env`. Future audits should re-grep the tree for any new secret
  fields that get added.

## Threat model

The orchestrator runs LLM-generated code on configured SSH targets.
Three layers of defense:

1. **Hardcoded blocklist** — `tools._TOOL_CMD_BLOCKLIST` rejects
   `rm -rf /`, `mkfs`, fork bombs, pipe-to-shell, etc. Cannot be
   overridden, no learning.
2. **Gates** — learned safety rules in [gates.py](gates.py). When the
   same class of failure happens N times (currently N=3, configurable),
   the pattern is auto-promoted to a blocking gate. Operator can list,
   review, toggle, or remove gates via the `/gates*` endpoints.
3. **Sudo allowlist** — `config.json:sudo.allowed_commands` restricts
   privileged commands to a specific whitelist (e.g. `apt-get install`,
   `systemctl start`). Sudo is opt-in per deploy.

## Path safety

- File path inputs from clients are validated by `SAFE_FILENAME` (no
  `..`, no `/`) and additionally by a `Path.resolve()` containment check
  in vault note handlers. SAFE_FILENAME was tightened in Phase 0.e.

## WebSocket cross-thread safety

Phase 0.e fixed `_ws_broadcast` to post coroutines onto the captured
main event loop via `asyncio.run_coroutine_threadsafe`, with a 2 s
timeout per send. Background threads (e.g. the run thread spawned by
`/orchestrate`) can broadcast safely.

## Dependencies

`requirements.txt` is a `pip freeze` snapshot of the venv at v0.1.0-phase0.
Run `pip-audit` periodically; rotate any flagged transitive deps.

## CI verification

Every push triggers `.github/workflows/ci.yml`: ruff + mypy + pytest.
Branch protection on `main` will require this to be green before merge
(once the repo is published).
