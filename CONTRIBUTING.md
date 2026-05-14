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
pytest -q                                                   # 599 tests, ~1 min
ruff check core llm notifications tools execution memory_pkg orchestration api references_pkg
mypy core llm notifications tools execution                 # lenient — non-blocking
```

The same set runs in CI on every PR (.github/workflows/ci.yml). 13
additional tests are gated behind the `prefect_real` and `redis_real`
markers and run against live services in their own CI jobs.

## Coverage

Coverage runs **nightly** via [.github/workflows/coverage.yml](.github/workflows/coverage.yml)
(not on every PR — `pytest-cov` + Prefect's per-test server spin-up
exhausts the GHA runner memory budget under the full instrumentation
scope; OOM-kill would block every merge).

Gate: `--cov-fail-under=40` over the **small-package** surface
(`core`, `llm`, `notifications`, `tools`, `references_pkg`, `agents`,
`manifest`, `cli`). The heavyweight packages (`orchestration/`,
`api/`, `evidence/`, `memory_pkg/`, `execution/`, `prefect_io/`) are
excluded until a parallel-coverage migration lands — tracked as a
Phase 4+ backlog item.

Aspirational target across the full surface is ≥80%. Anyone touching
a heavyweight package is encouraged to add tests; the nightly gate
keeps the small-package floor honest in the meantime.

The nightly job uploads `coverage.xml` to Codecov; the badge on the
README links to the trend graph.

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
