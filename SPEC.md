# Zephyr Commerce Platform — Specification (v0.1)

Status: **approved (v1.0)** · Owner: Zephyr · Language rule: everything in this project (code,
comments, logs, UI, messages, docs) is written in **English**.

This document is the single source of truth for what we build. Every work session starts by reading
`SPEC.md` and `PROGRESS.md`. Changes to behaviour are made here first, then in code.

---

## 1. Purpose

Replace the current "Eldorado API" tools (three copies of the same Flask app) with **one platform**
that:

- runs several stores (ZephyrStore, Kawai smurf, Jewelry today) from one panel with a store selector;
- sells on **several marketplaces** over time (Eldorado first; G2G, Z2U, PlayerAuctions… later);
- lets features be **switched on/off per store** from the UI (Top-Ups, Review Reminders, Restock, …);
- can absorb the other projects later (CS2 Farm bot, RL Account Checker, Price Sniper, Drops Checker)
  as modules instead of separate programs;
- is **trustworthy**: no double sales, no lost data, every money-moving path covered by tests and
  proven in shadow mode before it is allowed to act.

## 2. Non-negotiable invariants

Each invariant has at least one automated test. A change that breaks one is rejected.

| # | Invariant |
|---|-----------|
| I1 | A stock item (account, code, key…) is delivered **at most once**, ever. Enforced by the database (state machine + unique constraints), not only by application code. |
| I2 | An order is fulfilled **at most once**, across restarts, retries, timeouts and concurrent workers (idempotency key per order). |
| I3 | Stock items are **never hard-deleted**. They change state (`available → reserved → delivered`, or `quarantined`, `void`). Full history is kept. |
| I4 | When a marketplace call fails or times out, the system **re-reads the remote state before compensating** (never returns stock or re-delivers blindly). |
| I5 | Every action that moves money, stock or configuration writes an **audit log** entry (who/what, when, before/after, reason). |
| I6 | Remote access always requires authentication. No "trusted localhost via proxy headers" shortcuts. Login is rate-limited; sessions are revocable. |
| I7 | Secrets (API keys, webhook URLs, bot tokens, account passwords) are **encrypted at rest** and never returned in full by the API. |
| I8 | Configuration lives in the database, is validated by schema, and is versioned (every change is an audit entry and can be reverted). |
| I9 | A **dry-run / shadow mode** exists for every module that acts on a marketplace: it computes and records what it *would* do without doing it. |
| I10 | Automatic **daily backups** of the database with a tested restore procedure. |

## 3. Architecture

**Modular monolith**: one codebase, one database, several processes, strict module boundaries.
Simple to run and debug today, ready to split later if ever needed.

```
            Web panel (React SPA)        Discord bot        (future: mobile, CLI)
                     │                       │
                     └──────────┬────────────┘
                                │  HTTPS (REST + WebSocket), versioned /api/v1
┌───────────────────────────────▼─────────────────────────────────────────────┐
│ CORE                                                                         │
│  identity (users, roles, sessions) · stores · module registry · settings    │
│  stock (pools, items, secrets) · orders · fulfilment engine · audit log     │
│  job queue + scheduler · event bus · notification router · secrets vault    │
└──────┬──────────────────────┬───────────────────────┬───────────────────────┘
       │ MarketplaceConnector │ Module (feature)      │ NotificationChannel
       ▼                      ▼                       ▼
  eldorado (v1)          deliveries, restock,     discord_webhook (v1)
  g2g, z2u… (later)      topups, reviews,         discord_bot (v1.x)
                         chat_monitor, reports,   telegram… (later)
                         account_check, …
```

### 3.1 Processes (same codebase)

| Process | Responsibility |
|---------|----------------|
| `api` | HTTP API + WebSocket for the panel and bots. No long-running work. |
| `worker` | Executes jobs from the queue (deliveries, restock, polling, reports). Can run N replicas. |
| `scheduler` | Enqueues periodic jobs (poll orders every N s, daily report at 23:59 store time, …). Single instance (leader lock). |
| `bot` | Discord bot (optional). Talks to the core only through the internal API / event bus. |

### 3.2 Extension points (the reason this scales)

**MarketplaceConnector** — one per platform. The core never imports platform-specific code.

```python
class MarketplaceConnector(Protocol):
    key: str                                   # "eldorado"
    capabilities: set[Capability]              # ORDERS, DELIVER, CHAT, OFFERS, PRICE, VAULT, REVIEWS, CANCEL
    async def test_credentials(self) -> ConnectionStatus
    async def list_orders(self, since: datetime, states: set[OrderState]) -> list[RemoteOrder]
    async def get_order(self, remote_id: str) -> RemoteOrder
    async def mark_delivered(self, remote_id: str, payload: DeliveryPayload) -> RemoteResult
    async def cancel_order(self, remote_id: str, reason: str) -> RemoteResult
    async def list_offers(self) -> list[RemoteOffer]
    async def set_offer_price(self, remote_id: str, price: Money) -> RemoteResult
    async def set_offer_state(self, remote_id: str, state: OfferState) -> RemoteResult
    async def vault_list(self, offer_id: str) -> list[VaultEntry]          # instant-delivery secrets
    async def vault_add(self, offer_id: str, entry: VaultEntry) -> RemoteResult
    async def chat_list_messages(self, conversation_id: str, since: datetime) -> list[ChatMessage]
    async def chat_send(self, conversation_id: str, text: str) -> RemoteResult
    async def list_reviews(self, since: datetime) -> list[Review]
```

Connectors normalise everything into core types (`RemoteOrder`, `Money`, `OrderState`, …).
Every connector ships with a **fake implementation** used by tests and shadow mode.

**Module** — a feature package. It declares:

```python
class Module(Protocol):
    key: str                        # "topups"
    name: str                       # "Top-Ups"
    settings_schema: type[BaseModel]        # per-store settings, validated, rendered as a form
    requires: set[Capability]               # e.g. {ORDERS, CHAT}
    def register(self, app: ModuleContext) -> None:
        # routes, event subscriptions, scheduled jobs, UI pages, permissions
```

The UI lists every installed module per store with an **on/off switch** and its settings form.
A module whose required capabilities the store's marketplace lacks is shown as unavailable.

**NotificationChannel** — where alerts go (Discord webhook, Discord bot DM/channel, later Telegram,
email). Users create *notification rules*: event type + store filter + channel + template.

**Event bus** — modules communicate through domain events, never by calling each other directly.
Events are persisted (outbox table) so nothing is lost on restart.

### 3.3 Event catalogue (v1)

`order.received`, `order.paid`, `order.awaiting_confirmation`, `order.delivered`,
`order.delivery_failed`, `order.cancelled`, `order.refunded`, `stock.imported`, `stock.low`,
`stock.depleted`, `stock.quarantined`, `offer.restocked`, `offer.paused`, `offer.price_changed`,
`chat.message_received`, `review.received`, `report.daily_ready`, `system.error`,
`auth.login_failed`, `config.changed`.

## 4. Technology

| Area | Choice |
|------|--------|
| Language | Python 3.12 (backend, worker, bot) · TypeScript (frontend) |
| API | FastAPI, Pydantic v2, OpenAPI → generated TypeScript client |
| Database | PostgreSQL 16, SQLAlchemy 2 (async), Alembic migrations |
| Jobs | Database-backed queue (`jobs` table, `SELECT … FOR UPDATE SKIP LOCKED`), retries with backoff, dead-letter view |
| Realtime | WebSocket channel per store (order/stock/job updates) |
| Frontend | React 19, Vite, TanStack Query + Router, shadcn/ui (Radix) + Tailwind, react-hook-form + zod |
| Discord bot | discord.py 2.x (separate process) |
| HTTP client | httpx (timeouts, retries, rate limiting per connector) |
| Tests | pytest + pytest-asyncio, factory fixtures, fake connectors; Playwright for UI smoke tests |
| Quality | ruff, mypy (strict on core), pre-commit, GitHub Actions CI |
| Deployment | Docker Compose on the Linux VPS: `postgres`, `api`, `worker`, `scheduler`, `bot`, `caddy` (HTTPS + other websites) |
| Logs/metrics | Structured JSON logs, `/health` + `/ready`, error alerts as `system.error` events |

## 5. Repository layout

```
zephyr-platform/
  SPEC.md  PROGRESS.md  README.md
  docker-compose.yml  Caddyfile  .env.example
  backend/
    app/
      core/            # config, db, security, audit, events, jobs, settings, module registry
      identity/        # users, roles, sessions, 2FA
      stores/          # stores, marketplace connections, module toggles
      stock/           # pools, items, secrets, importer (multi-format parser)
      orders/          # orders, fulfilment engine, idempotency
      notifications/   # channels, rules, templates
      connectors/
        base.py        # MarketplaceConnector protocol + core types
        fake/          # fake connector (tests, shadow mode)
        eldorado/      # Eldorado + TalkJS implementation
      modules/
        deliveries/  restock/  topups/  reviews/  chat_monitor/  reports/  account_check/
      api/v1/          # routers (thin; call services)
      workers/         # worker + scheduler entrypoints
      bot/             # Discord bot entrypoint
    migrations/        # Alembic
    tests/
  frontend/
    src/ (app shell, pages per module, design system components)
  tools/
    migrate_legacy/    # importer from the old Eldorado API folders
```

## 6. Domain model (v1)

| Entity | Key fields / notes |
|--------|--------------------|
| `User` | email, password hash (argon2), role (`owner`, `operator`, `viewer`), 2FA optional |
| `Session` | user, created/last seen, IP, user agent, revocable |
| `Store` | name, slug, timezone, currency, active |
| `MarketplaceConnection` | store, connector key, encrypted credentials, status, last check |
| `StoreModule` | store, module key, enabled, settings (JSON validated by schema), version |
| `StockPool` | store (or shared), name, game, item type (`account`, `code`, `text`), low-stock threshold |
| `StockItem` | pool, encrypted payload (login, password, email…), fingerprint (hash of normalised login, **unique per pool and globally per store**), state (`available`, `reserved`, `delivered`, `quarantined`, `void`), position, timestamps, source import |
| `StockItemSecret` | extra secrets per item (2FA token, email password…) |
| `StockImport` | raw text hash, detected format, counts (ok / duplicates / rejected), user, time |
| `Listing` | store, connection, remote offer id, title, kind (`account`, `item`, `topup`), mapping to stock pool, delivery mode (`auto`, `assisted`, `text`, `manual`), units per sale (bundles), confirmation required (Twitch), template overrides |
| `Order` | store, connection, remote id (**unique**), listing, buyer, quantity, total, fees, state, remote state, idempotency key |
| `Fulfilment` | order (**unique**), state machine (`pending → reserved → remote_confirmed → buyer_notified → done` / `failed` / `compensated`), attempts, last error |
| `Reservation` | fulfilment, stock item (**unique while active**) |
| `VaultUpload` | listing, stock item (**unique**), remote entry id, state — replaces the old "upload ledger" |
| `Job` | type, payload, store, run_at, attempts, state, locked_by, last_error |
| `OutboxEvent` | type, payload, created, dispatched |
| `AuditLog` | actor, action, entity, before/after (secrets redacted), reason, time |
| `NotificationChannel` / `NotificationRule` | channel type + encrypted target, event filter, template |
| `Message` (chat) | conversation, direction, author, text, time, handled flags |

## 7. Modules

### 7.1 Stock (core)
- Pools per store, items with state machine (I1, I3).
- **Importer** (used a lot): paste text or upload a file → parser detects each line's format →
  **preview table** before saving: accepted / duplicate (already in stock or already delivered) /
  rejected with reason → confirm. Formats to support from day one (taken from the current parser):
  `user:pass`, `email:pass`, `user:pass:email:emailpass[:provider_url]`, numbered lists (`1. user:pass`,
  `2) …`), structured blocks (`name: … password: … token: …`), Russian-label logs
  (`ЛОГИН: … / Пароль: …`), supplier batch headers ignored (`Accounts (part 3/40) — order #…`).
  The parser is a pluggable list of format detectors, so a new supplier format = one new detector + test.
- Manual extraction (copy N accounts) goes through the same reservation path as deliveries.
- Low-stock thresholds raise `stock.low` / `stock.depleted`.

### 7.2 Deliveries (module, default on)
- Poll paid orders (and receive pushes if a marketplace offers them) → `order.paid`.
- Per listing delivery mode:
  - `auto`: reserve N = quantity × units-per-sale items atomically → call `mark_delivered` →
    send credentials in chat → commit. Any failure follows I4 (re-read order, then compensate or finish).
  - `assisted` (Twitch Drops): send confirmation request → wait for buyer reply → classify reply
    (confirm / cancel / question / unknown) → deliver, cancel, or escalate to a human. Unknown never acts.
  - `text` (tips, guides): deliver a configured text, no stock.
  - `manual`: notify only.
- Chat messages that fail to send are retried by the job queue until delivered (no "pending JSON file").
- Buyer-reply classification lives in a separate, tested component with per-language keyword packs
  (English built in; other languages as optional packs).

### 7.3 Restock (module)
- For listings that use the marketplace vault (instant delivery): keep the vault at the target size by
  uploading items from the mapped pool. Groups (PC / PlayStation / Xbox offers sharing one pool) are
  first-class. Upload = reserve item → `vault_add` → record `VaultUpload` → item `delivered` (I1).
- Resume paused/closed offers when stock returns; pause when the pool is empty (optional).

### 7.4 Top-Ups (module, per-store switch)
- Detect top-up orders; send the configured instructions message per listing; track sent/pending.

### 7.5 Review Reminders (module, per-store switch)
- After delivery + configurable delay, send one reminder per order; skip buyers who reviewed
  (configurable: skip negative only / skip all reviewed / include all); daily cap and humanised delays.

### 7.6 Chat Monitor (module)
- Poll conversations, emit `chat.message_received` with the anti-spam rules from the current system
  (ignore stale > 5 min, closed orders, seller-replied, seller active in last 3 min, 20-min cooldown).

### 7.7 Reports & statistics (module)
- Daily report at 23:59 store time: revenue, fees, net, orders, units, breakdown by product type and
  **by listing**, closing stock per pool, cancellations. Exactly once per store and day.
- The same numbers are visible in the panel (charts and tables) for any date range.

### 7.8 Account Check (module, low priority)
- Successor of the Drops Checker use-case: check an account's inventory/state on demand.

### 7.9 Notifications
- Channels: Discord webhook (v1), Discord bot (v1.x). Rules map events → channel → template.
- Templates with variables and live preview (the current "Discord Notification Center", rebuilt).

### 7.10 Discord bot (v1.x)
- Slash commands: `/stock [store]`, `/orders [store]`, `/order <id>`, `/deliver <id>` (with confirm
  button), `/pause <listing>`, `/report [date]`. Permission check against platform roles.

## 8. Marketplace: Eldorado connector (v1)

Endpoints already used by the current system (base `https://www.eldorado.gg`, OAuth2 client
credentials at `POST /api/authentication/seller/token`):

| Capability | Endpoint |
|-----------|----------|
| Profile | `GET api/users/me` |
| Orders | `GET api/orders/me/seller/orders` (pageSize, pageIndex, category, query), `GET api/orders/me/{id}`, `GET api/orders/me/statesCount` |
| Deliver / cancel | `PUT api/orders/me/{id}/deliver`, `POST api/orders/me/{id}/cancel` |
| Account offers | `GET api/flexibleOffers/me/search`, `GET api/flexibleOffers/{id}/private`, `GET api/flexibleOffersUser/me/stateCount` |
| Account offer actions | `PUT api/flexibleOffersUser/me/{id}/changePrice`, `/pause`, `/resume` |
| Vault (instant delivery) | `GET/POST api/flexibleOffersUser/me/offers/{id}/secretDetails`, `DELETE …/secretDetails/{entryId}` |
| Item offers | `GET api/v1/item-management/me/offers/me/search`, `…/state-count`, `PUT …/{id}/price`, `POST …/{id}/pause`, `POST …/{id}/resume` |
| Top-up offers | `GET api/predefinedOffers/me`, `PUT api/predefinedOffersUser/me/{id}/changePrice` |
| Reviews | `GET api/orders/reviews/{userId}`, `GET api/orders/reviews/{userId}/stats` |
| Chat | `GET api/conversations/me/authorize` → TalkJS token; TalkJS REST `GET/POST https://api.talkjs.com/v1/{appId}/conversations/{id}/messages` |

The connector handles token refresh, rate limits (429 → backoff), pagination, and maps Eldorado
order states (`Paid`, `Delivered`, `Received`, `Completed`, `Cancelled`, `Refunded`, `Disputed`…)
to core `OrderState`. Before implementation we record real sample responses (redacted) as test fixtures.

## 9. User interface

**Direction:** serious working tool. Neutral greys, one accent colour, no gradients or glows, line
icons, clear typography, dense but readable tables. Strong feedback: confirmation dialogs for
anything that moves stock or money ("Deliver 3 accounts to buyer X?"), toasts for results,
inline status badges, optimistic updates only where safe, undo where possible. Works on mobile.

**Information architecture:**
- Top bar: store selector (or "All stores"), global search (order id, buyer, login), user menu.
- **Overview**: today's sales, pending actions (orders awaiting confirmation, failed deliveries,
  low stock), recent activity feed.
- **Orders**: live list with filters; order detail with timeline (paid → reserved → delivered → chat sent).
- **Stock**: pools, item lists with state filters, **Import** (paste/upload → preview → confirm), history.
- **Listings**: offers per marketplace, mapping to pools, delivery mode, restock settings, prices.
- **Messages**: buyer conversations needing attention.
- **Reports**: daily/period stats, breakdown by listing and product type, export CSV.
- **Automation**: modules per store with on/off switches and settings forms.
- **Notifications**: channels, rules, templates with preview.
- **Settings**: stores, marketplace connections, users & roles, sessions, backups, audit log.

Design deliverable (stage 1): 2–3 visual directions as clickable mockups → one is chosen → design
tokens + component set.

## 10. Security

- Auth: email + password (argon2id), optional TOTP 2FA, session cookies (HttpOnly, Secure, SameSite=Lax),
  CSRF protection, login rate limit + lockout, session list with "log out everywhere".
- Roles: v1 ships only `owner`; `operator` (orders, stock, listings; no credentials/users) and `viewer` are reserved for later.
- Secrets encrypted with a key from the environment (never in the repo or database).
- Exposure: only Caddy (443) is public; Postgres and internal services are on a private network.
- Audit log is append-only.

## 11. Testing & release safety

- Unit tests for parsers, classifiers, state machines; integration tests with Postgres in Docker and
  the fake connector; concurrency tests for I1/I2 (many workers, same pool/order).
- **Shadow mode**: the platform reads real orders/offers but every write to the marketplace is
  replaced by a recorded "would do" decision; a comparison report against the old system runs daily.
- Cut-over per store with a documented rollback (old system kept ready for one week).

## 12. Migration from the current system

`tools/migrate_legacy` reads each old store folder (read-only) and imports:

| Old | New |
|-----|-----|
| `account_manager/accounts.sqlite3` → `games`, `categories`, `accounts`, `category_tokens` | `StockPool`, `StockItem`, `StockItemSecret` (expected counts: 5,707 / 6,513 / 186) |
| `extraction_history`, `restock_upload_ledger` | `StockItem` in state `delivered` (keeps duplicate protection) |
| `config.json` | `Store`, `MarketplaceConnection` (encrypted), notification channels |
| `item_delivery_config.json`, `restock_config.json` (incl. cross-platform groups), `topup_config.json`, `buyer_reminder_config.json` | `Listing` mappings + `StoreModule` settings |
| `discord_notifications_config.json` | `NotificationRule` + templates |
| `daily_report_cache.json`, `sales_monitor_cache.json` | "already reported / already notified" markers to avoid duplicates at cut-over |

The importer is idempotent (re-runnable) and prints a reconciliation report.

## 13. Deployment (Linux VPS, from December)

- `docker compose up -d` brings up `postgres`, `api`, `worker`, `scheduler`, `bot`, `caddy`.
- Caddy serves the panel (see 15.1 for access without a domain) and can host your other websites
  on the same server.
- Nightly `pg_dump` to a backups volume + off-server copy (e.g. object storage); restore drill documented.
- Updates: `git pull && docker compose up -d --build` with migrations applied automatically.

## 14. Roadmap

| Stage | Deliverable | Done when |
|-------|-------------|-----------|
| 0 | This spec approved | Owner sign-off |
| 1 | Visual direction + design system | Direction chosen |
| 2 | Foundation: repo, Docker, DB, migrations, auth, audit, jobs, events, module registry, fake connector, CI | CI green, login works |
| 3 | Stock + importer + legacy migration | Counts reconcile with the old databases |
| 4 | Eldorado connector (with recorded fixtures) | Contract tests pass against fixtures and a read-only live check |
| 5 | Deliveries module (auto, assisted, text) | Invariant tests (I1, I2, I4) pass under concurrency |
| 6 | Restock module | No-duplicate tests pass |
| 7 | Notifications (webhook) + chat monitor + reports | Messages verified on a test Discord channel |
| 8 | Top-Ups + Review Reminders (per-store switches) | Tests pass |
| 9 | Panel screens on the chosen design (desktop + mobile) | Full walkthrough |
| 10 | Shadow mode on the Linux VPS | 3 days with zero unexplained differences |
| 11 | Cut-over: ZephyrStore → Kawai smurf → Jewelry (owner's choice, 2026-11-23) | One stable week per store |
| 12+ | Discord bot, account check, absorb Price Sniper / CS2 / RL Checker, new marketplaces | — |

## 15. Decisions log

| Date | Decision |
|------|----------|
| 2026-11-14 | One panel for all stores with a store selector. |
| 2026-11-14 | Every optional module (Top-Ups, Review Reminders, …) is an on/off switch per store. |
| 2026-11-14 | Stack: Python + FastAPI, PostgreSQL, React + TypeScript, Docker Compose on the Linux VPS. |
| 2026-11-14 | The new platform launches directly on the Linux VPS (December); the old system keeps running on Windows until cut-over. |
| 2026-11-14 | Repository: `zephyr-rl/zephyr-platform` (private). |
| 2026-11-14 | Single user (owner) for now. The role model stays in the schema but v1 only ships the `owner` role. |
| 2026-11-14 | Domain: probably `zephyrstore.site` (not bought yet). Until then the panel is reached by IP (see 15.1). |
| 2026-11-14 | Future marketplaces to keep in mind: GameBoost, PlayerAuctions (if it has an API), G2G, G2A. **Nothing is built for them yet**; the connector contract just must not assume Eldorado-only concepts. |

### 15.1 Access without a domain

Opening the panel as `http://<ip>:<port>` would send the password and session in clear text. Options,
in order of preference:

1. **Private network (recommended):** Tailscale (free) on the VPS, PC and phone. The panel is only
   reachable inside that private network: nothing is exposed to the internet, and it works from 4G/5G.
2. **Cheap domain (~USD 10/year):** Caddy gets a real HTTPS certificate automatically.
3. **IP only with a self-signed certificate:** encrypted, but browsers show a warning every time.

Decision pending; it does not block stages 1–9.

## 16. Open questions

1. Access method for the panel (see 15.1).
2. Discord server/channels for alerts once the bot replaces webhooks (not needed until stage 12).
| 2026-11-23 | Cut-over order changed by the owner: ZephyrStore first, then Kawai smurf, then Jewelry. Each store needs 3 days of shadow mode with real mappings and no unexplained differences first (shadow restarted 2026-11-23 18:15 UTC, after the item deliveries were imported). |
