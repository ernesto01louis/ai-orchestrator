# Contributing

Stub — to be filled out during Phase 0.h. See [ROADMAP.md](ROADMAP.md) Phase 0.6.

## Local development (quickstart)

```bash
git clone <repo>
cd ai-orchestrator
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt  # (once pyproject.toml is authoritative, use pip install -e .)
cp config.example.json config.json
cp .env.example .env
# edit config.json and .env with your values
systemctl --user start ai-orchestrator  # or: uvicorn app:app --host 0.0.0.0 --port 8000
```

## Running the tests

```bash
pytest -q
```

Tests assume the orchestrator is running at `http://127.0.0.1:8000` (HTTP characterization tests) or use FastAPI's `TestClient` (in-process tests).

## Commit conventions

[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.

Branch naming: `feat/<slug>`, `fix/<slug>`, `refactor/<slug>`.

Never commit to `main` directly. PRs require passing CI.
