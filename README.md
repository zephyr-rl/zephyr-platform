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
  app/stock/       stock pools and items, multi-format importer, extraction, moves
  app/legacy/      read-only importer for the old stores' databases
  app/connectors/  connector protocol, Eldorado (seller API + chat), fake marketplace,
                   shadow (dry-run) wrapper
  app/listings/    marketplace offers and how each one is delivered
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

## Import the stock of the old stores

Reads an old `account_manager/accounts.sqlite3` read-only (work on a copy) and prints a
reconciliation per pool. Safe to run again: it only adds what is missing.

```bash
uv run python -m app.cli migrate-legacy --store zephyrstore \
    --db "path/to/Eldorado API - ZephyrStore/account_manager/accounts.sqlite3" --dry-run
```

Add `--quarantine-overlaps` to hold back accounts that are in stock but also appear in the old
extraction history. Requires `ZP_FINGERPRINT_KEY` (generate it once with `generate-key` and
never change it: duplicates are detected with it).

## Import the old notification and report settings

Reads an old store folder read-only: the Discord webhooks become notification channels
(encrypted), the old notifications become rules made from the English presets (keeping which
fields were shown and the offers switched off), and the chat cooldown, report time, time zone
and last reported day are copied. Safe to run again.

```bash
uv run python -m app.cli migrate-legacy-settings --store zephyrstore \
    --folder "path/to/Eldorado API - ZephyrStore" --dry-run
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

The full plan (install, import, shadow period with the daily comparison, cut-over store by
store, rollback) is in [docs/DEPLOY.md](docs/DEPLOY.md).

Only Caddy is exposed (ports 80/443). Nightly database dumps go to `./backups` (kept 14 days).
Keep a copy of `ZP_SECRET_KEYS` outside the server: without it, stored credentials cannot be read.
