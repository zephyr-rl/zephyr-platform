# Progress

Read `SPEC.md` first. This file says where we are and what comes next.

## Current stage
**Stage 4 — Eldorado connector** (next).

## Next
1. Stage 4: Eldorado connector with recorded fixtures (orders, offers, vault, chat, reviews),
   read-only live check, listings mapped to stock pools.
2. Stage 5: deliveries module (auto, assisted, text) with the I1/I2/I4 concurrency tests.

## Decisions
- 2026-11-14: **Accounts in stock that also appear in the old extraction history**
  (ZephyrStore 157, Kawai 5, Jewelry 0) stay *available*, as in the old system. The owner
  decided not to quarantine them, so the cut-over migration runs without
  `--quarantine-overlaps`.

## Done
- 2026-11-14: `SPEC.md` approved (v1.0).
- 2026-11-14: Repository pushed to GitHub (`zephyr-rl/zephyr-platform`).
- 2026-11-14: Stage 1 — visual direction **Graphite** chosen
  (canvas: https://claude.ai/artifact/3WEgxpEEBm3gzSxo1Lz8Py). Tokens in `DESIGN.md` and
  `frontend/src/styles/tokens.css`.
- 2026-11-14: **Stage 2 — Foundation.**
  - Backend: owner login (argon2id, HttpOnly session cookie, CSRF token, login rate limit,
    session list / revoke / log out everywhere), stores, marketplace connections with
    AES-256-GCM encrypted credentials (key rotation supported), per-store module switches
    with schema-validated, versioned settings, append-only audit log (database trigger),
    transactional outbox + event bus, PostgreSQL job queue (SKIP LOCKED, retries with backoff,
    dead-letter, stale-lock recovery, idempotency keys), connector protocol + fake marketplace +
    shadow (dry-run) wrapper, worker and scheduler (leader lock, idempotent periodic runs),
    JSON logs, `/health` and `/ready`, Alembic initial migration.
  - Panel (Graphite): sign-in, app shell with store selector, Overview, Automation (module
    switches with undo, settings forms generated from the schemas), Settings (stores, shadow
    mode with confirmation, connections, sessions, audit log). Works at phone width.
  - Quality: 51 backend tests against PostgreSQL (invariants I5, I6, I7, I8, I9; concurrent
    workers never run a job twice), 6 frontend tests, ruff + mypy strict, browser walkthrough.
  - Delivery: Dockerfile, `docker-compose.yml` (postgres, migrate, api, worker, scheduler,
    nightly backup, Caddy), GitHub Actions CI.

- 2026-11-14: **Stage 3 — Stock.**
  - Pools and items: payload AES-GCM encrypted, login fingerprint (HMAC) unique per store in any
    state, so an account sold or removed can never be imported and sold again. Twitch tokens as
    encrypted secrets. Append-only item history. Database triggers: items are never deleted,
    `delivered` and `void` are final.
  - Importer for every supplier format (colon/pipe/slash/spaces, tokens, e-mail credentials,
    numbered and glued lists, Russian labels, name/password blocks, supplier headers) with
    preview → confirm (guarded by the text hash).
  - FIFO extraction with copy/download and undo, quarantine/release/void, moves within and across
    stores, low-stock and depleted events, search by exact login.
  - `migrate-legacy`: verified on copies of the three stores — 5,707 / 6,513 / 186 accounts and
    1,391 / 1,391 Twitch tokens reconcile; history imported as 1,762 / 2,564 / 3 "extracted"
    items; a second run creates nothing. (Run again on fresh copies at cut-over.)
  - Panel: Stock (pools, search, recent extractions), pool page (filters, import preview,
    extract, bulk actions, item history), Overview shows stock and low pools.

## Notes
- New stores start in **shadow mode**: nothing is sent to a marketplace until it is turned off
  (with a confirmation dialog).
- Only the fake (testing) marketplace exists until Stage 4.
