"""Nightly database dump for the Windows install (invariant I10).

Run by the scheduled task "Zephyr Platform backup". Writes
``<root>\\backups\\zephyr-YYYYMMDD-HHMM.dump`` (pg_dump custom format,
restorable with pg_restore) and deletes dumps older than 14 days.
Settings lists them. Copy them off the server too.

    python -m app.deploy.backup --root C:\\ZephyrPlatform
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.deploy import envfile

KEEP_DAYS = 14


def find_pg_dump(root: Path) -> str:
    exe = "pg_dump.exe" if sys.platform == "win32" else "pg_dump"
    for candidate in (root / "pgsql" / "bin" / exe,):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("pg_dump")
    if not found:
        raise FileNotFoundError("pg_dump was not found (expected in <root>\\pgsql\\bin)")
    return found


def dump(root: Path, *, now: datetime | None = None, pg_dump: str | None = None) -> Path:
    values = envfile.read(root / ".env")
    url = make_url(values.get("ZP_DATABASE_URL") or os.environ["ZP_DATABASE_URL"])
    folder = root / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"zephyr-{(now or datetime.now()):%Y%m%d-%H%M}.dump"
    partial = target.with_suffix(".dump.tmp")
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    argv = [
        pg_dump or find_pg_dump(root),
        "-h",
        url.host or "127.0.0.1",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "zephyr",
        "-Fc",
        "-f",
        str(partial),
        url.database or "zephyr",
    ]
    result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")
    os.replace(partial, target)
    return target


def prune(folder: Path, *, keep_days: int = KEEP_DAYS, now: float | None = None) -> list[Path]:
    cutoff = (now or time.time()) - keep_days * 86400
    removed = []
    for path in folder.glob("zephyr-*.dump"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    for path in folder.glob("zephyr-*.dump.tmp"):
        path.unlink()
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.deploy.backup")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        target = dump(root)
    except (RuntimeError, FileNotFoundError, KeyError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    removed = prune(root / "backups")
    print(
        f"Backup written: {target.name} ({target.stat().st_size} bytes); {len(removed)} old removed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
