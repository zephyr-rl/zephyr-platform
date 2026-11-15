ZEPHYR PLATFORM - WINDOWS SERVER KIT

1. Copy this whole folder to the server (for example to C:\ZephyrKit).
2. Double click INSTALL.bat and accept the administrator prompt.
   It installs into C:\ZephyrPlatform, next to the old bots, without touching them.
   It asks once for your e-mail and a password (12+ characters) for the panel.
3. Open http://localhost:8600 on the server (shortcut "Zephyr Platform" on the desktop).

Every store starts in SHADOW MODE: nothing is sent to Eldorado, everything is only recorded.

Later versions: copy the new kit folder to the server and double click UPDATE.bat in it.

In C:\ZephyrPlatform:
  START.bat / STOP.bat   start or stop the panel and the workers
  CHECK.bat              health check
  REMOTE_ACCESS.bat      temporary https link for the phone (Cloudflare, like the old system)
  SNAPSHOT_OLD.bat       copy the old stores' data into legacy\ (read only) for imports
  zephyr.bat             administrative commands (import, daily comparison)
  .env                   the keys: keep a copy OUTSIDE the server

Full guide: DEPLOY-WINDOWS.md
