# Review: work done while the owner was away (2026-11-14, from 17:00)

The owner asked to continue stage by stage without approvals and to document every change so it
can be reviewed and changed afterwards. This page lists, per stage, what changed, the decisions
taken without asking, and how each can be changed. Details of each stage are in PROGRESS.md.

Unchanged rules, respected throughout: no bot was started with the real credentials; nothing
was sent to Eldorado (only the fake marketplace and recordings were used); the old system and
its backups were only read; everything in the project is in English.

## Discord test messages (original webhooks)

- Asked: configure the original webhooks and send test messages to them.
- Done: the platform's import (`migrate-legacy-settings`) was run against copies of the three
  stores' real settings files: 5 channels and 9 rules were created as expected (ZephyrStore
  sales + chat, Kawai smurf sales + chat, Jewelry one channel used for both).
- Not sent from here: this environment cannot reach Discord, and moving the webhook URLs through
  the session was blocked as secret-handling. Instead, a script was left on the owner's PC:
  `Proyectos en la VPS/_tmp_claude/send_discord_tests.py`. Run `python send_discord_tests.py` from
  that folder: it reads the URLs from the old store folders on the PC and posts one "Test
  message" per channel plus one "[Test]" example of each notification (sale, buyer message,
  daily report), marked "Test message with example values: not a real order". It was tried
  against a local fake webhook; it changes nothing else.
- The sanitized copies used for the check are in `_tmp_claude/legacy_settings/` (no Eldorado
  credentials in them). They can be deleted.

## Stage 8: top-ups and review reminders

| Decision | Why | To change it |
|---|---|---|
| A top-up is never marked delivered by the platform: instructions are sent, then the order waits in Orders → Needs you ("Top-up: waiting for you") until it is marked delivered on Eldorado. | The owner does the top-up by hand; the old portal also only sent the welcome message. | Tell me if the platform should mark it delivered after you confirm in the panel (a "Done" button). |
| Which top-up offers get instructions is set per listing (Listings → top-up listing → switch), stored in the Top-Ups module. The old per-offer settings are imported. | One place per listing, like the other delivery settings. | — |
| Review reminders start in **manual mode** (you pick the buyers and press "Send"); automatic mode is a setting. | The old tool was started by hand; sending messages to buyers is visible to them. | Automation → Review reminders → Automatic. |
| A batch never goes past the daily limit (old "max messages per batch" = 100 for ZephyrStore). | Account safety (rate of messages). | Automation → Review reminders → Daily cap. |
| Nothing is queued or sent while a store is in shadow mode (candidates are shown). | Shadow mode must not send. | — |
| Orders the old tool already reminded (207 for ZephyrStore) are imported so they are never reminded again. | No buyer gets the message twice. | — |

## Stage 9: panel screens

- New: global search in the top bar (order id, buyer, or an account's exact login); Overview
  with today's sales, a "To do" list and recent activity; Settings → change password, Backups.
- Checked on a phone (390 px): no page scrolls sideways.
- **Not done, waiting for the owner's decision:**
  - "All stores" in the store selector: every page works per store today (Notifications is
    already global). It is a big change across pages; worth it only if you want one combined
    view.
  - Two-factor sign-in (TOTP): optional in the spec; easy to add if wanted.

## Stage 10: shadow mode on the VPS (prepared, not started)

Starting it needs the VPS and the owner. Prepared:

- `docs/DEPLOY.md`: the full plan (install, import, one week of shadow mode with a daily
  comparison, cut-over store by store, rollback).
- `compare-shadow` command: what the old system did vs what the platform would have done.
- `migrate-legacy --sync-removed`: before the cut-over, accounts sold by the old system during the
  shadow period are taken out of sale in the platform. **Important:** without this step, they
  would be on sale twice. It is in the cut-over steps.
- CI now starts the whole production stack and checks it answers.

## Smaller changes

- Notification test messages from a rule use the rule's store name and say they are examples.
- Automation: settings with a fixed set of choices are shown as a select.
- HTTP client logs are silenced (they would show webhook URLs).
- Found by the new CI test of the whole stack: opening the panel by IP address (no domain)
  failed at the TLS handshake. Fixed with `ZP_DEFAULT_SNI` (set it to the server IP when there
  is no domain; documented in `.env.example` and DEPLOY.md). CI now starts the stack exactly as
  on the VPS and gets the panel and the API through Caddy.

## Owner's answers (2026-11-15)

- Discord test script: run; the messages were sent.
- "All stores": added to Overview and Reports only. Two-factor sign-in: not wanted.
- No Linux VPS yet: Stage 10 runs on the Windows Server 2022 remote desktop where the old bots
  run (16 GB RAM, 6 vCPU, 140 GB disk).

## Windows Server kit (2026-11-15)

- Built for the remote desktop instead of the Linux VPS: native install, no Docker (Docker
  Desktop does not run on Windows Server). Details in docs/DEPLOY-WINDOWS.md.
- Choices you may want to change: install folder `C:\ZephyrPlatform`, panel port 8600,
  database port 5433, 2 workers, nightly backup at 04:17 server time, phone access through a
  Cloudflare quick tunnel (same as the old system) instead of a fixed domain.
- Verified for real on a Windows Server 2022 machine in CI (install, sign-in, backup, check,
  reinstall, update, stop, start). Not yet run on your server.

## What I needed from you (answered)

1. Run `send_discord_tests.py` on the PC and tell me if the messages look right (14 in total: one test per channel, and per store an example sale, buyer message and daily report).
2. Decide on "All stores" and two-factor sign-in (Stage 9 leftovers).
3. When you want to start Stage 10: access to the VPS (or run DEPLOY.md steps 1–2 yourself).
