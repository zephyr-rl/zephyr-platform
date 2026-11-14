# Progress

Read `SPEC.md` first. This file says where we are and what comes next.

## Current stage
**Stage 2 — Foundation** (not started). Blocked in the current cloud session: package installs
(FastAPI, SQLAlchemy, Alembic, React/Vite) need network access for pip/npm.

## Next
1. Stage 2 — Foundation: backend skeleton (FastAPI, settings, DB session, Alembic, auth),
   job queue + outbox, fake connector, frontend shell (Vite + React + tokens), CI.
2. Stage 3 — Stock + importer + legacy migration.

## Done
- 2026-11-14: `SPEC.md` approved (v1.0).
- 2026-11-14: Repository pushed to GitHub (`zephyr-rl/zephyr-platform`).
- 2026-11-14: Three visual directions published for review
  (canvas: https://claude.ai/artifact/3WEgxpEEBm3gzSxo1Lz8Py).
- 2026-11-14: Owner chose **Graphite**. Tokens and feedback rules in `DESIGN.md`,
  code tokens in `frontend/src/styles/tokens.css`, component sheet on the canvas.
