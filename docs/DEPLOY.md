# Running the platform on the VPS: shadow mode, then cut-over

This is the plan for stages 10 and 11. Everything up to "Start" can be done in an afternoon; the
shadow period then runs for about a week next to the old system, store by store.

Nothing here touches the old system or its files: they are copied and read.

## 1. Server

- Linux VPS with Docker Engine and the Compose plugin (`docker compose version`).
- Ports 80 and 443 open. Nothing else needs to be public.
- A domain pointing to the server (for example `zephyrstore.site`) gets a real certificate
  automatically. Without a domain, use the IP with a self-signed certificate (see `.env`).

## 2. Install

```bash
git clone https://github.com/zephyr-rl/zephyr-platform.git
cd zephyr-platform
cp .env.example .env
docker compose build
docker compose run --rm --no-deps api python -m app.cli generate-key   # → ZP_SECRET_KEYS
docker compose run --rm --no-deps api python -m app.cli generate-key   # → ZP_FINGERPRINT_KEY
nano .env      # POSTGRES_PASSWORD, ZP_SECRET_KEYS, ZP_FINGERPRINT_KEY, ZP_SITE_ADDRESS, ZP_TLS_MODE
               # (and ZP_DEFAULT_SNI=<server IP> when there is no domain)
mkdir -p backups
docker compose up -d
docker compose run --rm api python -m app.cli create-owner --email you@example.com
```

Keep a copy of `.env` outside the server (for example in your password manager). Without
`ZP_SECRET_KEYS` the stored credentials cannot be read; without `ZP_FINGERPRINT_KEY` duplicate
detection breaks. Never change `ZP_FINGERPRINT_KEY` once stock is imported.

Check: open the site, sign in, Overview loads. Settings → Backups lists the folder (empty until
the first night).

## 3. Stores (all start in shadow mode)

In the panel, Settings:

1. Create the three stores: ZephyrStore (America/Argentina/Buenos_Aires), Kawai smurf
   (Europe/Madrid), Jewelry (America/Argentina/Buenos_Aires). New stores are in **shadow mode**:
   nothing is sent to Eldorado, everything is only recorded.
2. Add the Eldorado connection of each store (Client ID and secret). "Check" must say Connected.
   After the cut-over, create new credentials in Eldorado and replace these.

## 4. Copy the old data and import it

Copy each old store folder to the server, read-only for the platform, for example to
`/srv/legacy/Eldorado API - ZephyrStore/`. Then, per store (example for ZephyrStore):

```bash
# Stock: check first, then import.
docker compose run --rm -v /srv/legacy:/legacy:ro api python -m app.cli migrate-legacy \
  --store zephyrstore --db "/legacy/Eldorado API - ZephyrStore/account_manager/accounts.sqlite3" --dry-run
docker compose run --rm -v /srv/legacy:/legacy:ro api python -m app.cli migrate-legacy \
  --store zephyrstore --db "/legacy/Eldorado API - ZephyrStore/account_manager/accounts.sqlite3"

# Discord channels, notification layout, chat cooldown, report time, top-ups, review reminders.
docker compose run --rm -v /srv/legacy:/legacy:ro api python -m app.cli migrate-legacy-settings \
  --store zephyrstore --folder "/legacy/Eldorado API - ZephyrStore"
```

The reports must say "OK, counts reconcile". Then in the panel:

- Listings → "Read offers from marketplace", then per listing: pool, delivery mode, restock.
- Automation: switch on the modules the old system ran for that store.
- Notifications: the imported channels do **not** post for stores in shadow mode (the old
  system is still posting). Use "Send test" to check each channel once.

## 5. Shadow period (stage 10)

All three stores run in shadow mode for about a week while the old system keeps selling.

Every day, per store, compare what the old system did with what the platform would have done
(fresh copy of the old logs first):

```bash
docker compose run --rm -v /srv/legacy:/legacy:ro api python -m app.cli compare-shadow \
  --store zephyrstore --folder "/legacy/Eldorado API - ZephyrStore" --hours 24
```

It lists the orders delivered by both, by only one of them, and those with a different number
of accounts; the same for top-up instructions and vault uploads. It exits with an error code
when there are differences, so it can run from cron and alert:

```cron
30 9 * * * cd /root/zephyr-platform && docker compose run --rm -v /srv/legacy:/legacy:ro api python -m app.cli compare-shadow --store zephyrstore --folder "/legacy/Eldorado API - ZephyrStore" >> /var/log/zephyr-shadow.log 2>&1
```

Every difference gets an explanation (written in PROGRESS.md) or a fix. Also check each day:
Orders (simulated deliveries look right), Messages, Reports against the old daily report.

During the shadow period, add new stock **only in the old system** (it is the one selling). It
is brought over at the cut-over.

**Exit criterion:** a full week with no unexplained difference, for every store.

## 6. Cut-over, one store at a time (stage 11)

For the store being moved, in this order:

1. Stop the old system's bots for that store (deliveries, restock, top-ups, reminders, chat
   and sales monitors). The old system stays installed and its data untouched (rollback).
2. Copy its folder again, then bring the stock up to date. `--sync-removed` takes out of sale
   every account imported earlier that the old system sold or removed meanwhile, so nothing is
   sold twice; new accounts added there are imported:
   ```bash
   docker compose run --rm -v /srv/legacy:/legacy:ro api python -m app.cli migrate-legacy \
     --store zephyrstore --db ".../accounts.sqlite3" --sync-removed
   docker compose run --rm -v /srv/legacy:/legacy:ro api python -m app.cli migrate-legacy-settings \
     --store zephyrstore --folder "/legacy/Eldorado API - ZephyrStore"
   ```
3. Settings → turn off shadow mode for the store (confirmation dialog).
4. Watch the first orders end to end: Orders, the buyer's chat on Eldorado, the Discord sale
   message, stock counts.
5. Rotate the Eldorado credentials (new Client secret in Eldorado, replace it in the panel).

**Rollback** (within a week): turn shadow mode back on for the store, start the old bots again.
Accounts the platform delivered meanwhile are listed in Stock → the pool → "Delivered"; remove
them from the old system's stock before restarting it.

## 7. Afterwards

- Copy `./backups` off the server regularly (Settings → Backups shows the latest dumps and warns
  when there is none in the last 36 hours).
- Updates: `git pull && docker compose up -d --build` (migrations run automatically).
