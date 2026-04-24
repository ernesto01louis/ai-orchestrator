# Security

Stub — to be filled out during Phase 0.h.

## Secret handling

- Secrets (API tokens, passwords) live in `.env` (gitignored), never in `config.json` or source.
- `config.json` contains structure and non-sensitive settings; a safe template is provided at `config.example.json`.
- `.env.example` lists the environment variables the orchestrator expects.

## Threat model (outline)

The orchestrator executes LLM-generated code on configured SSH targets. The three-layer safety model (see [VISION.md](VISION.md)):

1. Hardcoded blocklist (non-negotiable; `rm -rf /`, `mkfs`, pipe-to-shell, etc.).
2. Gates — learned safety rules promoted from repeated failures. See [gates.py](gates.py).
3. Sudo allowlist — privileged operations restricted to a specific set of commands.

## Reporting a vulnerability

TODO (0.h).
