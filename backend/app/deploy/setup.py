"""Install steps for Windows that are easier (and testable) in Python. Called by install.ps1.

python -m app.deploy.setup init-env --root C:\\ZephyrPlatform --port 8600 --pg-port 5433
python -m app.deploy.setup init-db  --root C:\\ZephyrPlatform
python -m app.deploy.setup has-owner --root C:\\ZephyrPlatform      (exit 0 = an owner exists)
python -m app.deploy.setup check    --root C:\\ZephyrPlatform      (health check, readable)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.engine import make_url

from app.deploy import envfile
from app.deploy.supervisor import Paths, environment

SAFE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")
APP_ROLE = "zephyr"
APP_DB = "zephyr"


def init_env(root: Path, *, port: int, pg_port: int) -> dict[str, str]:
    """Create or complete <root>/.env. Existing values are never replaced (the keys especially)."""
    path = root / ".env"
    values = envfile.read(path)
    defaults = {
        "ZP_ENV": "production",
        "ZP_PORT": str(port),
        "ZP_WORKERS": "2",
        "ZP_PG_PORT": str(pg_port),
        "ZP_PG_SUPERPASSWORD": envfile.new_password(),
        "ZP_SECRET_KEYS": envfile.new_key(),
        "ZP_FINGERPRINT_KEY": envfile.new_key(),
    }
    for key, value in defaults.items():
        values.setdefault(key, value)
    if "ZP_DATABASE_URL" not in values:
        values["ZP_DATABASE_URL"] = (
            f"postgresql+asyncpg://{APP_ROLE}:{envfile.new_password()}"
            f"@127.0.0.1:{values['ZP_PG_PORT']}/{APP_DB}"
        )
    root.mkdir(parents=True, exist_ok=True)
    envfile.write(path, values)
    return values


def _literal(password: str) -> str:
    if not SAFE.match(password):
        raise ValueError("Unexpected characters in the generated password.")
    return f"'{password}'"


async def create_database(values: dict[str, str], *, admin_dsn: str | None = None) -> list[str]:
    """Create the app role and database if missing; keep the role's password as in .env."""
    url = make_url(values["ZP_DATABASE_URL"])
    password = url.password or ""
    dsn = admin_dsn or (
        f"postgresql://postgres:{values['ZP_PG_SUPERPASSWORD']}@127.0.0.1:{values['ZP_PG_PORT']}/postgres"
    )
    done = []
    conn = await asyncpg.connect(dsn)
    try:
        role = url.username or APP_ROLE
        db = url.database or APP_DB
        exists = await conn.fetchval("select 1 from pg_roles where rolname = $1", role)
        verb = "alter" if exists else "create"
        await conn.execute(f'{verb} role "{role}" with login password {_literal(password)}')
        done.append(f"role {role} {'updated' if exists else 'created'}")
        if not await conn.fetchval("select 1 from pg_database where datname = $1", db):
            await conn.execute(
                f'create database "{db}" owner "{role}" encoding \'UTF8\' template template0'
            )
            done.append(f"database {db} created")
    finally:
        await conn.close()
    return done


def migrate(root: Path) -> None:
    paths = Paths(root)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=paths.backend,
        env=environment(paths),
        check=True,
    )


async def has_owner(values: dict[str, str]) -> bool:
    url = make_url(values["ZP_DATABASE_URL"]).set(drivername="postgresql")
    conn = await asyncpg.connect(url.render_as_string(hide_password=False))
    try:
        return bool(await conn.fetchval("select count(*) from users"))
    finally:
        await conn.close()


def check(root: Path) -> int:
    """Readable health check: what works and what does not."""
    problems = 0

    def line(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(f"  [{'OK' if ok else '!!'}] {text}")

    values = envfile.read(root / ".env")
    line(bool(values.get("ZP_SECRET_KEYS")), ".env with the keys")
    try:
        line(asyncio.run(has_owner(values)), "database answers and has an owner account")
    except Exception as exc:
        line(False, f"database: {exc}")
    port = values.get("ZP_PORT", "8600")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/ready", timeout=5) as r:
            line(r.status == 200, f"panel answers on http://localhost:{port}")
    except Exception as exc:
        line(False, f"panel on port {port}: {exc}")
    dumps = sorted((root / "backups").glob("zephyr-*.dump"), key=lambda p: p.stat().st_mtime)
    if dumps:
        age = (time.time() - dumps[-1].stat().st_mtime) / 3600
        line(age < 30, f"last backup {dumps[-1].name}, {age:.0f} hours ago")
    else:
        print("  [--] no backup yet (the first one runs tonight)")
    stop = root / "run" / "stop"
    line(not stop.exists(), "no stop file (run\\stop makes everything stop)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.deploy.setup")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init-env", "init-db", "has-owner", "check"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
        if name == "init-env":
            p.add_argument("--port", type=int, default=8600)
            p.add_argument("--pg-port", type=int, default=5433)
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    if args.command == "init-env":
        init_env(root, port=args.port, pg_port=args.pg_port)
        print(f"Settings in {root / '.env'} (keep a copy of this file outside the server).")
        return 0
    values = envfile.read(root / ".env")
    if args.command == "init-db":
        for _ in range(30):  # the database service may still be starting
            try:
                for step in asyncio.run(create_database(values)):
                    print(step)
                break
            except (OSError, asyncpg.CannotConnectNowError):
                time.sleep(2)
        else:
            print("The database does not answer.", file=sys.stderr)
            return 1
        os.environ.update(values)
        migrate(root)
        print("Database ready.")
        return 0
    if args.command == "has-owner":
        return 0 if asyncio.run(has_owner(values)) else 1
    return check(root)


if __name__ == "__main__":
    sys.exit(main())
