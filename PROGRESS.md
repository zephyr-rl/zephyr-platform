# Progress

Read `SPEC.md` first. This file says where we are and what comes next.

## Current stage
**Stage 10 — Shadow mode on the VPS** (next; needs the VPS: prepared up to that point). Stage 7 waits for one check by the owner:
a test message on a Discord test channel (acceptance criterion of Stage 7).

## Next
1. Owner: create a test channel in Discord, add its webhook in Notifications and press
   "Send test" (and "Send test" on a rule) to confirm the messages arrive.
2. Stage 10: install on the VPS in shadow mode next to the old system (see docs/DEPLOY.md).

## Decisions
- 2026-11-14: **Accounts in stock that also appear in the old extraction history**
  (ZephyrStore 157, Kawai 5, Jewelry 0) stay *available*, as in the old system. The owner
  decided not to quarantine them, so the cut-over migration runs without
  `--quarantine-overlaps`.
- 2026-11-14: **Spanish buyer replies are recognised in every store by default**
  (`reply_languages = ["en", "es"]`), at the owner's request for ZephyrStore, Kawai smurf and
  Jewelry. A store can still turn it off in Automation → Deliveries.
- 2026-11-14: **Restock has no separate "offer groups".** Listings that share a stock pool
  (the PC / PlayStation / Xbox offers of the old groups) draw from the same accounts, and the
  stock locks make sure an account goes to one vault only.
- 2026-11-15: **No two-factor sign-in** (owner's decision): sign-in stays with the password
  only. **"All stores" only in Overview and Reports** (owner's decision), as a switch on those
  two pages; every other page works per store.
- 2026-11-15: **Stage 10 runs on the Windows Server 2022 remote desktop** (where the old bots
  run) until the Linux VPS exists: native install without Docker (see docs/DEPLOY-WINDOWS.md).
- 2026-11-14: **A top-up is never marked delivered by the platform.** When the Top-Ups module is
  on, the instructions are sent and the order waits in Orders → Needs you ("Top-up: waiting for
  you") until the owner does it and marks it delivered on the marketplace.
- 2026-11-14: **Review reminders start in manual mode** (as the old tool): the owner picks the
  buyers and starts each batch; automatic mode is a setting. A batch never goes past the day's
  limit. Nothing is queued or sent while the store is in shadow mode.
- 2026-11-14: **Owner away (from 17:00): stages continue without approvals.** Every change is
  listed in `docs/REVIEW.md` so it can be reviewed and changed afterwards.
- 2026-11-14: **Notifications are not posted for stores in shadow mode** unless the channel says
  so ("Also post for stores in shadow mode"): during the parallel run the old system already
  announces every sale, so the same sale would be posted twice.
- 2026-11-14: **Revenue is the order total.** The Eldorado seller API does not return fees, so
  reports show revenue, not net.
- 2026-11-14: **Notification texts are English presets.** The import of the old settings keeps
  the channels, the shown fields, the colours and the switched-off offers, not the old texts.
- 2026-11-14: **An upload whose outcome is unknown and is not in the vault afterwards is
  quarantined, not returned to stock**: it may have been stored and sold already. The owner
  checks it before putting it back.

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
  - Reply classifier: English built in, Spanish as a per-store pack (on by default since the
    decision of 2026-11-14).
  - Kill switch: "Deliver automatically" off holds every automatic listing for approval.
  - Shadow mode: records what would happen (mode, confirmation, stock available) and touches no
    stock and sends nothing (I9).
  - Orders screen: "Needs you" / "In progress" / "All", order timeline, approve (with
    confirmation), retry, mark handled by hand (unsent accounts go back to stock), attention
    badge in the menu, "Check for new orders".
  - Design note: credentials are sent before marking the order delivered (the order the old
    portal used in production), so the items are committed as delivered the moment they leave.

- 2026-11-14: **Stage 6 — Restock.**
  - Per listing (account offers only): "Keep the vault stocked", accounts kept in the vault
    (or the store default), resume the offer when it was paused with an empty vault. Delivery
    stays Manual on these listings (the marketplace delivers from the vault), and paid orders on
    them are only recorded.
  - A pass per listing, under a database lock (two workers never upload to the same vault):
    read the vault → resolve earlier uploads with an unknown outcome → top up from the pool →
    resume the offer if it was paused and something was uploaded.
  - Every upload is a `vault_uploads` row (history: never deleted, a resolved row is final),
    committed as "pending" with the account reserved *before* calling the marketplace. Success:
    the account is delivered for good (I1). Refused: back to stock. Unknown (timeout): stays
    pending and is resolved by reading the vault on the next pass.
  - Accounts already in the vault are quarantined instead of uploaded twice.
  - Shadow mode records "would upload" / "would resume" and touches no stock (I9).
  - Runs every minute; each listing is checked when its interval (store setting) has passed.
    "Restock now" in Listings runs one listing immediately, after a confirmation dialog that
    says the accounts leave the stock for good (or that nothing is sent, in shadow mode).
  - Listings screen: vault count against the target, last result, recent uploads (login hint
    and state only, never passwords).
  - Tests: 205 backend (19 new: top-up, refill after a sale, e-mail details, store default,
    empty pool, duplicates, refused / unknown / unknown-but-stored uploads, resume rules, shadow
    mode, 4 concurrent passes, restock and chat delivery sharing a pool, history triggers,
    validation, interval, API). Browser walkthrough on the fake marketplace.

- 2026-11-14: **Stage 7 — Notifications, chat monitor, reports.**
  - Notifications: Discord webhook channels (URL encrypted, shown as a hint), rules = event +
    store (or all) + channel + template, with English presets in the layout of the old
    notifications (sale, buyer message, daily report, delivery problem, low / empty stock,
    vault restocked). Template editor with variables and a live Discord-like preview, "Send
    test" per channel and per rule, log of every message with "Send again" for failures.
  - Delivery: one message per (event, rule), unique in the database, so a re-dispatched event
    never posts twice; sent by a job that follows Discord's rate limit (retry_after) and retries
    outages with backoff; a deleted webhook stops and is shown on the channel. Credentials never
    appear in a message. HTTP client logs are silenced (they contain the webhook URL).
  - Instant-delivery sales (from the vault) are announced once when first seen, if recent.
  - Chat monitor (module, on by default, read-only): the old anti-spam rules (new and at most
    5 minutes old, not answered, seller not active in the last 3 minutes, 20-minute cooldown,
    courtesy replies on delivered orders ignored, automatic messages are not the seller,
    CONFIRM / CANCEL left to deliveries), alert words mark a message urgent. Messages page with
    the conversations waiting for a reply, "Mark handled", and a badge in the menu.
  - Reports (module): numbers for any range in the store's time zone (revenue, orders,
    accounts delivered, cancellations, by product type, by listing, stock), CSV export (safe
    against spreadsheet formulas), Reports page with a revenue-per-day chart (with table view).
    Daily report at the store's time, exactly once per store and day (database), sent up to
    3 hours late if the platform was down at that time.
  - `migrate-legacy-settings`: imports the old Discord webhooks, notification layout, chat
    cooldown, report time, time zone (Kawai smurf: Europe/Madrid) and last reported day.
  - Tests: 242 backend (37 new), 7 frontend. Browser walkthrough with a fake Discord.

- 2026-11-14: **Stage 8 — Top-ups and review reminders.**
  - Top-ups (module): when a top-up order is paid, the instructions are sent once in the order
    chat (the listing's own message, else the imported per-offer message, else the module's
    default) and the order goes to the new state "Top-up: waiting for you" (Needs you). When the
    owner marks it delivered on the marketplace, it is done; if the buyer cancels, cancelled.
    The platform never marks a top-up delivered. Before sending again after a failed attempt the
    chat is read (I4). Listings: per top-up listing, "Send the top-up instructions" and its own
    message. Module off, or the offer switched off: nothing is sent (manual).
  - Review reminders (module): candidates = delivered/completed orders at least `delay_hours`
    and at most `lookback_days` old, never reminded, one per buyer within the cooldown, after
    the review filter (skip negative / skip reviewed / remind all, read from the marketplace's
    reviews). One reminder per order ever (database). Sent one at a time through the job queue
    with a random pause (20-50 s by default), at most `daily_cap` a day, re-checking the review
    right before sending; committed as "sending" before the message goes out and checked in the
    chat after an interruption. Retryable errors: up to 3 tries. Page "Review reminders": today's
    count against the limit, the buyers to remind (select and send, with confirmation), empty the
    queue, history.
  - `migrate-legacy-settings` also imports `topup_config.json` (default and per-offer messages,
    offers switched off; `{buyerUsername}` → `{buyer}`, `{productName}` → `{product}`,
    `{orderId}` → `{order}`) and `buyer_reminder_config.json` / `buyer_reminder_history.json`
    (settings, and every order already reminded so it is never reminded again).
  - Automation: settings with a fixed set of choices are shown as a select; settings edited per
    listing are shown as a count with a pointer to Listings.
  - Tests: 268 backend (26 new), 7 frontend. Browser walkthrough (top-up in Needs you, listing
    switch, Automation, reminders queued and sent).

- 2026-11-14: **Stage 9 — Panel screens, desktop and mobile.**
  - Top bar: global search (orders by id or buyer; stock accounts only by their exact login,
    also when pasted as login:password, since logins are stored as fingerprints). Enter or click
    opens the order, or the account's history in its pool (deep links `/orders?order=` and
    `/stock/<pool>?item=`).
  - Overview: today's revenue, orders, accounts delivered and orders that need you; a "To do"
    list (orders that need you, buyers waiting, low pools, failed jobs); recent activity of the
    store (from the audit log).
  - Settings: change password (signs out every other session), backups (the nightly dumps,
    with an alert when the newest is older than 36 hours; the folder is mounted read-only into
    the API container).
  - Phones: every page checked at 390 px wide, no horizontal scrolling; row actions wrap under
    the text.
  - Not done (left for the owner to decide): an "All stores" choice in the store selector (each
    page works per store; Notifications is already for all stores), and optional two-factor
    sign-in (TOTP).
  - Tests: 272 backend (4 new), 7 frontend; desktop and mobile walkthrough.

- 2026-11-14: **Stage 10 prepared (up to the point that needs the VPS).**
  - `docs/DEPLOY.md`: install on the VPS, import, shadow period with the daily comparison and
    its exit criterion, cut-over store by store, rollback.
  - `compare-shadow`: compares the old system's logs (deliveries with their number of accounts,
    top-up instructions, vault uploads per offer) with the platform's simulated deliveries and
    shadow decisions for the same period; never reads the delivered messages (they hold
    credentials); says when the old logs (about 200 entries) do not cover the period; exits
    with an error when there are differences (for cron).
  - `migrate-legacy --sync-removed`: before the cut-over, takes out of sale every account
    imported earlier that is no longer in the old stock (sold or removed by the old system
    during the shadow period), matched by login so an account moved to another category there
    is kept. Without it, those accounts would be on sale twice.
  - CI starts the whole production stack (`docker compose up`) and checks the API and the panel
    answer through Caddy. It found that access by IP (no domain) failed at the TLS handshake:
    browsers send no server name to an IP; fixed with `ZP_DEFAULT_SNI` in the Caddyfile.
  - Tests: 275 backend (3 new).

- 2026-11-15: **All stores in Overview and Reports.**
  - A "<store> / All stores" switch on both pages (remembered per page in the browser).
  - Overview, all stores: today's totals, one row per store (revenue, orders, accounts, orders
    that need you, buyers waiting, low pools; each number opens that store's page), activity
    of every store with its name.
  - Reports, all stores: the same numbers added up, each store's days in its own time zone
    (Kawai smurf: Madrid), plus "By store"; listings and pools show their store. The CSV export
    stays per store.
  - API: `GET /all/summary`, `GET /all/overview`.
  - The old system's SQLite database is opened read-only with its path quoted (Windows folders
    have spaces).
- 2026-11-15: **Windows Server kit** (Stage 10 runs on the remote desktop, next to the old bots).
  - `scripts/build_windows_kit.py` builds the kit: backend, built panel, `deploy/windows/`
    scripts, docs/DEPLOY-WINDOWS.md. `INSTALL.bat` / `UPDATE.bat` (elevate themselves).
  - Install into `C:\ZephyrPlatform`: uv + Python 3.12, PostgreSQL 16 (EDB installer,
    unattended, service ZephyrPostgres, port 5433, localhost only), `.env` with generated keys
    (never replaced; readable by Administrators and SYSTEM only), database and owner account,
    scheduled tasks "Zephyr Platform" (supervisor at boot + every 5 minutes, one instance) and
    "Zephyr Platform backup" (04:17, 14 days kept). Panel on `127.0.0.1:8600` only; the old
    ports 5000/7000/8000 are untouched.
  - `app/deploy/`: `supervisor.py` (runs migrations, then the API, 2 workers and the scheduler;
    restarts with backoff; rotated logs; stops on `run\stop`), `backup.py` (pg_dump, prune),
    `setup.py` (init-env, init-db, has-owner, check), `run.py` (zephyr.bat: CLI with the
    install's settings), `legacy_snapshot.py` (SNAPSHOT_OLD.bat: read-only copy of
    `C:\EldoradoBots\Eldorado API - *`; SQLite through its backup API; backup folders skipped).
  - The worker and the scheduler also stop when `ZP_STOP_FILE` exists (no SIGTERM on Windows).
  - `create-owner --password-env` for unattended installs.
  - CI job `windows-kit` (windows-2022) builds the kit and runs INSTALL, sign-in, backup,
    CHECK, a second INSTALL (keys unchanged), UPDATE, STOP and START for real.
- 2026-11-15: **Installer: owner password asked by PowerShell** with a `*` per character and
  a repeat; the first real install on the server stopped there because Python's hidden prompt
  showed nothing while typing. The password reaches `create-owner` through a temporary
  environment variable that is removed right after.
- 2026-11-15: **Time zone picker** in Settings > Stores: type a city, country or offset
  (`utc-03`), pick from the list (offset shown). `GET /timezones` lists the tz database the
  backend uses (Region/City names plus UTC, old duplicate names of the same city hidden);
  aliases a browser reports are still accepted. On the server the owner had typed
  "Buenos Aires/Argentina", which is not a tz name.
- 2026-11-16: **Messages: CONFIRM / CANCEL replies and "thanks" no longer count as waiting.**
  Seen on the server (3 threads: 2 ZephyrStore, 1 Kawai smurf): buyers' "confirm" on orders
  the old system had already delivered and answered. In shadow mode there is no fulfilment
  here, so only the alert was silenced, not the "waiting" count. Now `chat_threads.no_reply_needed`
  is worked out on every scan (CONFIRM/CANCEL in any enabled language; a thanks on a delivered
  or completed order), also for threads of orders that are no longer scanned.
- 2026-11-16: **Update over SSH stopped half way once** (platform down about a minute, no
  data touched): with all output redirected, Windows PowerShell turned uv's progress lines
  on stderr into errors after the platform was already stopped. Running UPDATE again brought
  it back. The scripts now judge native programs by their exit code only, and CI runs UPDATE
  with redirected output.
- 2026-11-16: **Old data imported on the server** (over SSH; old folders only read).
  - `legacy_snapshot`: copies of `C:\EldoradoBots\Eldorado API - *` in
    `C:\ZephyrPlatform\legacy` (folder restricted to Administrators and SYSTEM).
  - `migrate-legacy` (dry run first, then real; reports in `legacy\reports`): Zephyr 5669
    accounts in 19 pools, 1769 history items; Kawai 6461 in 4 pools, 2616 history items;
    Jewelry 179 in 2 pools, 3 history items. All "OK, counts reconcile". In stock and also in
    the old history: 157 (Zephyr) + 5 (Kawai) = 162, left available as decided.
  - `migrate-legacy-settings`: 5 Discord channels, 3 rules per store, chat cooldown, report
    time, last reported day (2026-11-15), Zephyr top-up messages and 221 already-reminded
    orders. Jewelry's time zone set to America/Argentina/Buenos_Aires from the old config
    (it had been created with Europe/Madrid). Channels do not post while stores are in shadow
    mode.
- 2026-11-16: Owner's decisions: the 162 accounts in stock and in the old history stay
  available (confirmed again after seeing where they came from: mostly category renames and
  moves, Oct 2 - Nov 10). Jewelry's time zone set to **Europe/Madrid** (audited store update);
  running `migrate-legacy-settings` again for Jewelry would put the old config's Buenos Aires
  back, so do not re-run it without checking.
- 2026-11-16: **Cross-platform cards** (the old system's "crossPlatformGroups"; missed in the
  first plan, pointed out by the owner).
  - `listing_groups` + `listings.group_id/device`. A card joins the PC, PlayStation and Xbox
    offers of one product, one stock pool, one vault target per platform, the extra text,
    e-mail details and auto-resume. The card copies its settings to its listings, so restock
    keeps working per listing (same locks, same never-sold-twice rules), all from one pool.
    A member listing refuses pool/vault changes of its own ("change it on the card").
  - A card can wait for an offer not synced yet; "Read offers from marketplace" attaches it.
  - API `/stores/{id}/listing-groups` (list, create, update, on/off, delete, restock now).
    Deleting a card leaves the offers, with restock off.
  - Panel: Listings → "Cross-platform cards" (platform state, vault n/target, pool stock,
    on/off, Restock now, editor); member rows show "Card · PC" and "Edit card".
  - Sale notifications get `{platform}` (PC / PlayStation / Xbox); the sale preset shows it.
  - `migrate-legacy-restock`: restock module (on/off, interval rounded up to minutes, default
    target), cards (matched by the old group id on re-runs), single offers with a category.
    "Manual only" offers are left alone; extra-text presets are not imported (already in each
    card).
- 2026-11-16: **Restock setup imported on the server** (`migrate-legacy-restock`, dry run
  first): Zephyr 6 cards (all 18 offers attached; restock module on, every 1 min), Kawai 1
  card (restock module off, as in the old system), Jewelry 1 card (module on, every 2 min).
  Kawai's and Jewelry's offers attach when their offers are read from the marketplace.
  Stores stay in shadow mode: restock only records what it would upload.
  `zephyr.bat` now prints reports as UTF-8 (offer names with emoji broke the Windows console).
- 2026-11-16: **Listings split in tabs, like the old system** (owner's request, plan approved).
  - Tabs Accounts (default) / Custom items / Top-ups, with counts; the tab is in the address
    (`/listings?tab=items`). Search by title, state filter (All / Active / Paused / Closed)
    and "Show offers gone from Eldorado" (off by default) on every tab.
  - Accounts: "Automatic restock" switch in the header (the store's Restock module, same as
    Automation → Restock). Turning it off is immediate; turning it on in a live store asks
    first. Then the cross-platform cards and the account offers table.
  - Custom items and Top-ups: compact cards (no images, owner's decision). Item card: price,
    units on Eldorado, pool and accounts left (or "Message only"), delivery mode, units per
    sale, CONFIRM, a switch for automatic delivery (off = Manual), first line of the
    message, Eldorado link, Edit. Top-up card: price, platform, units, "Send instructions"
    switch, own or general message, "N wait for you" linking to Orders filtered to that offer.
  - Listings store the offer's platform (Eldorado `tradeEnvironmentValues`); the listings API
    returns `platform` and `orders_need_you`; `GET /orders?listing_id=`.
- 2026-11-16: **Automation trimmed, switches moved to Listings** (owner's decisions).
  - Removed: Account check (it did nothing). Restock and Top-Ups are no longer listed in
    Automation (`managed_in="listings"`); they still run as modules.
  - Listings header: "Automatic deliveries" (the Deliveries kill switch, every tab), on
    Accounts "Automatic restock" with the check interval (1-60 min), on Top-ups "Top-up
    instructions" and "General message". Turning any of them on in a live store asks first.
  - Restock has no store default target any more: a card or offer without its own keeps 1
    (migration strips `target_quantity`; imported cards keep their 5 or 2).
  - Deliveries is always on (orders feed reports, top-ups, chat monitor and reminders): its
    switch is gone and the API refuses to turn it off.
  - Automation now says "settings for <store>": messages and settings are per store. Cards no
    longer stretch to the height of their neighbour (Reports was very tall).

## Notes
- New stores start in **shadow mode**: nothing is sent to a marketplace until it is turned off
  (with a confirmation dialog).
- Only the fake (testing) marketplace exists until Stage 4.
