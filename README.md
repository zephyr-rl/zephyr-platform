# Zephyr Platform

Management platform for marketplace stores (Eldorado first; other marketplaces later).
Read [`SPEC.md`](SPEC.md) for the design and [`PROGRESS.md`](PROGRESS.md) for the current stage.
Visual rules: [`DESIGN.md`](DESIGN.md).

## Layout

```
backend/    FastAPI API, worker, scheduler (Python 3.12, PostgreSQL 16)
  app/core/        config, database, crypto, audit log, events (outbox), jobs, module registry
  app/identity/    owner login, sessions, rate limit
  app/stores/      stores, marketplace connections, per-store module switches
  app/connectors/  connector protocol, fake marketplace, shadow (dry-run) wrapper
  app/modules/     feature modules (declared; behaviour arrives stage by stage)
  app/workers/     worker and scheduler processes
  migrations/      Alembic
  tests/           pytest, against a real PostgreSQL
frontend/   React panel (Vite, TanStack Router + Query, Tailwind, Graphite tokens)
```

## Run it locally

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22, PostgreSQL 16.

```bash
# 1. Backend
cd backend
uv sync
cp ../.env.example .env                      # then set the values below
#   ZP_DATABASE_URL=postgresql+asyncpg://postgres:<password>@127.0.0.1:5432/zephyr
#   ZP_SECRET_KEYS=<output of: uv run python -m app.cli generate-key>
#   ZP_COOKIE_SECURE=false                   # only for http://localhost
uv run alembic upgrade head
uv run python -m app.cli create-owner --email you@example.com --name "Your name"
uv run uvicorn app.main:app --reload --port 8000

# 2. Worker and scheduler (separate terminals)
uv run python -m app.workers.worker
uv run python -m app.workers.scheduler

# 3. Panel
cd ../frontend
npm install
npm run dev                                  # http://localhost:5173 (proxies /api to :8000)
```

## Tests and checks

```bash
cd backend
createdb zephyr_test                         # once
uv run pytest -q                             # ZP_TEST_DATABASE_URL overrides the test database
uv run ruff check . && uv run mypy app

cd ../frontend
npm run typecheck && npm test && npm run build
```

CI runs all of the above on every push (`.github/workflows/ci.yml`).

## Deploy (Linux VPS)

```bash
cp .env.example .env                         # fill in POSTGRES_PASSWORD, ZP_SECRET_KEYS, ZP_SITE_ADDRESS
docker compose up -d --build
docker compose run --rm api python -m app.cli create-owner --email you@example.com
```

Only Caddy is exposed (ports 80/443). Nightly database dumps go to `./backups` (kept 14 days).
Keep a copy of `ZP_SECRET_KEYS` outside the server: without it, stored credentials cannot be read.
