# Contributing

## Local development

Single-host, single-LXC for now. Bring a fresh LXC up with Python 3.11+,
then:

```bash
git clone <this repo> /opt/ai-orchestrator
cd /opt/ai-orchestrator
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-mock httpx ruff mypy
cp config.example.json config.json
cp .env.example .env
# edit config.json (Ollama URLs, SSH targets) and .env (GOTIFY_TOKEN, etc.)
uvicorn app:app --host 0.0.0.0 --port 8000   # or via systemd unit
```

## Running checks before pushing

```bash
pytest -q                                                   # 26 tests, < 1s
ruff check core llm notifications tools execution memory_pkg orchestration api references_pkg
mypy core llm notifications tools execution                 # lenient — non-blocking
```

The same set runs in CI on every PR (.github/workflows/ci.yml).

## Commit conventions

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
`fix:`, `refactor:`, `docs:`, `test:`, `chore:`. Branch names follow
`<type>/<short-slug>` (e.g. `feat/campaign-abstraction`,
`refactor/split-execution-submodules`).

Never commit to `main`. Open a PR; CI must be green; one approval
required (yes, even solo — forces the second look).

## Where things go

- A new endpoint → `api/routes.py` (until the per-area sub-split lands).
  Add a characterization test in `tests/test_smoke_http.py`.
- A new tool → entry in `tool_registry.json` (no code change needed if
  it's a simple shell command).
- A new agent role → `agents/<role>/` with its own
  `system_prompt.md` / `user_prompt.md` / `schema.json`. Wire it in
  `orchestration/` if it participates in the run loop.
- A new memory layer field → write/read test, vault note update,
  briefing inclusion, **and** update [CLAUDE.md](CLAUDE.md).

## What NOT to add

See [CLAUDE.md](CLAUDE.md) "Do NOT build" section. Domain-specific
code (aero, RF, anything that references specific hardware) belongs in
a consumer project, never here.

If you're unsure whether something belongs in orchestrator vs. a
consumer project, default to the consumer project. The orchestrator
stays minimal and generic on purpose — see [VISION.md](VISION.md).

## Reporting bugs

Open a GitHub issue with: orchestrator version (`git rev-parse HEAD`
or the `v0.x.x` tag), the failing endpoint or function, the curl /
Python snippet that reproduces, and the relevant log lines from
`journalctl -u ai-orchestrator`.
