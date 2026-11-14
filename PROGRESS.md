# Progress

Read `SPEC.md` first. This file says where we are and what comes next.

## Current stage
**Stage 6 — Restock** (next).

## Next
1. Stage 6: restock module — keep the Eldorado vault of account listings stocked from their
   pools (vault uploads recorded, item delivered on upload, I1), offer groups (PC / PS / Xbox
   sharing a pool), pause/resume offers with stock. Still in shadow mode.
2. Stage 7: notifications (Discord bot / webhooks), chat monitor, reports.

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

- 2026-11-14: **Stage 4 — Eldorado connector and listings.**
  - `app/connectors/eldorado`: seller API + TalkJS chat behind the connector protocol. OAuth
    token cached and refreshed on 401, per-connection rate limit, reads retried on
    timeouts/429/5xx, writes never retried blindly; failed deliveries, cancels and vault uploads
    re-read the remote state first (I4). Orders queried per state (the API returns one page of
    50), order lookup by exact id only (the old portal could return a different order), unknown
    states never mapped to known ones, offers of every kind with kind-specific endpoints.
  - 27 contract tests on fixtures (no network), including "shadow mode sends no write".
    Fixtures are built from the field names the old portal used; `eldorado-record` refreshes
    them from the live API with GET requests only and redacts secrets.
  - Listings: read-only sync of every offer, fulfilment settings per listing (stock pool,
    manual / assisted / automatic / message-only, accounts per unit, Twitch confirmation,
    messages, vault info, e-mail details). New listings start as **manual**.
  - `open_connector()` applies shadow mode at run time: writes become `shadow.<action>` audit
    entries and never reach the marketplace.
  - Panel: Listings screen (read offers, edit delivery per offer), Eldorado credentials form.
  - 2026-11-14: checked against a read-only, redacted recording of the live API for the three
    stores (the owner ran `_tests_claude/eldorado_record.py`; GET requests only). Fixed from it:
    order lists are paged with a cursor (the old code thought one page of 50 was all), order
    lookup by id is not available to the seller API (403) so orders are searched in the list,
    reviews are nested in `reviews.results[].orderReview`, chat text comes in a `content`
    array, and the `Buyer_Does_Not_Need_It_Anymore` cancel reason exists. All recorded orders
    (102), offers (51), reviews (100) and messages (10) parse with no unknown states.

- 2026-11-14: **Stage 5 — Deliveries.**
  - Orders polled per store every minute (cursor pages), recorded once; each new paid order
    gets exactly one fulfilment (I2: unique per order, row locked while a step runs).
  - Fulfilment steps commit one at a time: reserve (no partial reservations; one active
    reservation per item, enforced by the database) → send credentials in the chat → items
    delivered (I1: once sent they never come back) → mark delivered on the marketplace.
    Before sending, the order is re-read and the chat is checked, so a lost answer never causes
    a second send (I4). Marketplace errors retry with backoff; after 6 tries or a final error
    the order stops as "needs attention".
  - Modes: manual, message only, automatic, assisted (owner approves), plus the Twitch buyer
    confirmation (CONFIRM / CANCEL; one clarification, then a person; the owner writing in the
    chat stops automation). Messages are the ones buyers already received from the old portal.
  - Reply classifier: English built in, Spanish as an optional per-store pack (off by default).
  - Kill switch: "Deliver automatically" off holds every automatic listing for approval.
  - Shadow mode: records what would happen (mode, confirmation, stock available) and touches no
    stock and sends nothing (I9).
  - Orders screen: "Needs you" / "In progress" / "All", order timeline, approve (with
    confirmation), retry, mark handled by hand (unsent accounts go back to stock), attention
    badge in the menu, "Check for new orders".
  - Design note: credentials are sent before marking the order delivered (the order the old
    portal used in production), so the items are committed as delivered the moment they leave.

## Notes
- New stores start in **shadow mode**: nothing is sent to a marketplace until it is turned off
  (with a confirmation dialog).
- Only the fake (testing) marketplace exists until Stage 4.
