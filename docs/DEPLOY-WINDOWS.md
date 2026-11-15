# Running the platform on the Windows Server (until the Linux VPS exists)

The platform runs on the same Windows Server 2022 as the old bots (16 GB RAM, 6 vCPU, 140 GB
disk), without Docker. The old bots, their folders and their backups are not touched: the
platform only **reads** a copy of the old data, and every store starts in **shadow mode**.

When the Linux VPS exists, move with a database dump and the `.env` file (section 9).

## 1. What gets installed

Everything lives in `C:\ZephyrPlatform` (the `-Root` option changes it):

| Folder / item | What it is |
|---|---|
| `app\backend`, `app\panel` | the program and the built panel (replaced by each update) |
| `venv`, `python`, `tools\uv.exe` | Python 3.12 and the packages, installed by uv |
| `pgsql`, `pgdata` | PostgreSQL 16, Windows service **ZephyrPostgres**, port **5433**, this machine only |
| `logs` | `supervisor.log`, `api.log`, `worker-1.log`, `worker-2.log`, `scheduler.log`, `migrations.log` (rotated, 5 × 20 MB) |
| `backups` | nightly dumps `zephyr-YYYYMMDD-HHMM.dump`, 14 days kept (listed in Settings) |
| `legacy` | read-only copies of the old stores' data (`SNAPSHOT_OLD.bat`) |
| `.env` | keys and database passwords; readable by Administrators and SYSTEM only |
| Task **Zephyr Platform** | starts the supervisor at boot and checks every 5 minutes that it runs |
| Task **Zephyr Platform backup** | nightly dump at 04:17 (server time) |

The panel listens on `127.0.0.1:8600` only. Ports in use by the old portals (5000, 7000, 8000)
are not touched; the installer refuses to start if 8600 or 5433 are taken (`-Port`, `-PgPort`).

Expected use: about 1 GB of RAM in total (PostgreSQL, the API, 2 workers, the scheduler) and a
few GB of disk, so the old bots keep their room.

## 2. Install

1. Copy the kit folder to the server (RDP copy and paste works), for example `C:\ZephyrKit`.
2. Double click `INSTALL.bat`, accept the administrator prompt.
3. It downloads uv, Python 3.12 and PostgreSQL 16 (about 400 MB the first time). Without
   internet for PostgreSQL: put `postgresql-16.x-1-windows-x64.exe` in the kit's `downloads`
   folder first.
4. It asks for your e-mail and a password (12+ characters) for the panel.
5. At the end it opens nothing by itself: use the desktop shortcut or http://localhost:8600.

**Copy `C:\ZephyrPlatform\.env` to a safe place outside the server** (a password manager, an
encrypted USB stick). Without `ZP_SECRET_KEYS` the stored Eldorado credentials and Discord
webhooks cannot be read; without `ZP_FINGERPRINT_KEY` duplicate detection breaks.

Running `INSTALL.bat` again is safe: it never replaces the keys or the database.

## 3. Use from the phone

`REMOTE_ACCESS.bat` opens a Cloudflare quick tunnel, like the old `START_REMOTE_TUNNEL.bat`. It
prints an `https://….trycloudflare.com` link that works while the window is open; the link
changes each time. The panel asks for the password as always (5 wrong tries per e-mail in 15
minutes block that e-mail for a while).

## 4. Stores and connections

In the panel, Settings:

1. Create the three stores: ZephyrStore (America/Argentina/Buenos_Aires), Kawai smurf
   (Europe/Madrid), Jewelry (America/Argentina/Buenos_Aires). They start in shadow mode.
2. Add each store's Eldorado connection (Client ID and secret). "Check" must say Connected.
   The platform only reads from Eldorado while a store is in shadow mode.

## 5. Import the old data

1. `SNAPSHOT_OLD.bat` copies `C:\EldoradoBots\Eldorado API - *` into
   `C:\ZephyrPlatform\legacy\` (JSON files as they are, SQLite databases with SQLite's own
   backup, read-only, safe while the old bots run). Folders named like backups are skipped.
2. In `C:\ZephyrPlatform`, open a command prompt **as administrator** and, per store
   (example for ZephyrStore):

```bat
cd /d C:\ZephyrPlatform
zephyr.bat migrate-legacy --store zephyrstore --db "legacy\Eldorado API - ZephyrStore\account_manager\accounts.sqlite3" --dry-run
zephyr.bat migrate-legacy --store zephyrstore --db "legacy\Eldorado API - ZephyrStore\account_manager\accounts.sqlite3"
zephyr.bat migrate-legacy-settings --store zephyrstore --folder "legacy\Eldorado API - ZephyrStore"
```

Store slugs: `zephyrstore`, `kawai-smurf`, `jewelry` (as shown in Settings). The reports must
say "OK, counts reconcile". Then in the panel: Listings → "Read offers from marketplace", and per
listing pool, delivery mode and restock; Automation: the modules the old system ran.
Imported Discord channels do not post for stores in shadow mode (the old system still posts).

## 6. Shadow week (stage 10)

Each day: `SNAPSHOT_OLD.bat`, then per store

```bat
zephyr.bat compare-shadow --store zephyrstore --folder "legacy\Eldorado API - ZephyrStore" --hours 24
```

It lists the orders delivered by both systems, by only one, or with a different number of
accounts, and the same for top-ups and vault uploads (exit code 1 when there are differences).
Every difference gets an explanation in PROGRESS.md or a fix. New stock goes **only into the
old system** during this week. Exit criterion: a full week without unexplained differences.

## 7. Day to day

- `CHECK.bat`: database service, task, processes, panel answering, last backup.
- `STOP.bat` stops the panel and the workers **until `START.bat`** (also across reboots: the
  file `run\stop` stays). The database keeps running.
- Logs: `C:\ZephyrPlatform\logs`.
- Update: copy the new kit to the server, run its `UPDATE.bat`. It makes a backup, stops the
  platform (workers finish their current job), copies the new version, updates the packages,
  starts again and brings the database up to date. Keys and data stay.

## 8. Backups and restore

Dumps are in `C:\ZephyrPlatform\backups`; copy them off the server regularly. Restore one
(this replaces the current data):

```bat
STOP.bat
cd /d C:\ZephyrPlatform\pgsql\bin
set PGPASSWORD=<ZP_PG_SUPERPASSWORD from .env>
dropdb -h 127.0.0.1 -p 5433 -U postgres zephyr
createdb -h 127.0.0.1 -p 5433 -U postgres -O zephyr zephyr
pg_restore -h 127.0.0.1 -p 5433 -U postgres -d zephyr --no-owner --role=zephyr C:\ZephyrPlatform\backups\zephyr-YYYYMMDD-HHMM.dump
START.bat
```

## 9. Cut-over and moving to Linux later

- Cut-over per store (stage 11) is the same as in [DEPLOY.md](DEPLOY.md) section 6, with
  `SNAPSHOT_OLD.bat` + `zephyr.bat migrate-legacy ... --sync-removed` instead of the
  `docker compose run` lines.
- Moving to the Linux VPS: take a fresh dump (`venv\Scripts\python.exe -m app.deploy.backup
  --root C:\ZephyrPlatform`, run from `app\backend`), copy it and the values of
  `ZP_SECRET_KEYS` and `ZP_FINGERPRINT_KEY` to the VPS `.env`, `pg_restore` it there, then
  follow DEPLOY.md.

## 10. Uninstall

`STOP.bat`; delete the tasks "Zephyr Platform" and "Zephyr Platform backup" (Task Scheduler);
uninstall PostgreSQL with `C:\ZephyrPlatform\pgsql\uninstall-postgresql.exe`; delete
`C:\ZephyrPlatform` after copying `backups` and `.env` somewhere safe.
