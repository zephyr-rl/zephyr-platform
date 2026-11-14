"""Administrative commands.

python -m app.cli create-owner --email you@example.com --name "Owner"
python -m app.cli generate-key
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.core.audit import Actor
from app.core.config import get_settings
from app.core.crypto import generate_key
from app.core.platform import build_platform
from app.identity import service as identity
from app.identity.models import User


async def create_owner(email: str, name: str, password: str) -> None:
    platform = build_platform(get_settings())
    try:
        async with platform.db.sessionmaker() as session:
            if await session.scalar(
                select(User.id).where(User.email == identity.normalise_email(email))
            ):
                sys.exit(f"A user with e-mail {email} already exists.")
            await identity.create_user(
                session,
                email=email,
                password=password,
                display_name=name,
                actor=Actor.system("cli"),
            )
            await session.commit()
    finally:
        await platform.db.dispose()
    print(f"Owner {email} created.")


async def migrate_legacy(
    slug: str, db: Path, *, dry_run: bool, quarantine_overlaps: bool, json_out: Path | None
) -> bool:
    from app.legacy.migrate import format_report, migrate
    from app.stores.models import Store

    platform = build_platform(get_settings())
    try:
        async with platform.db.sessionmaker() as session:
            store_id = await session.scalar(select(Store.id).where(Store.slug == slug))
            if store_id is None:
                sys.exit(f"No store with slug {slug!r}. Create it in the panel first.")
            report = await migrate(
                session,
                platform,
                Actor.system("migrate-legacy"),
                db_path=db,
                store_id=store_id,
                dry_run=dry_run,
                quarantine_overlaps=quarantine_overlaps,
            )
    finally:
        await platform.db.dispose()
    print(format_report(report))
    if json_out:
        await asyncio.to_thread(
            json_out.write_text, json.dumps(report.as_dict(), indent=2), encoding="utf-8"
        )
    return report.ok


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("create-owner", help="Create the owner account")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", default="Owner")
    sub.add_parser("generate-key", help="Print a new secret key for ZP_SECRET_KEYS")
    legacy = sub.add_parser(
        "migrate-legacy", help="Import an old store's accounts.sqlite3 (read-only)"
    )
    legacy.add_argument("--store", required=True, help="Slug of the store in the platform")
    legacy.add_argument("--db", required=True, type=Path, help="Path to the old accounts.sqlite3")
    legacy.add_argument("--dry-run", action="store_true", help="Report only, save nothing")
    legacy.add_argument(
        "--quarantine-overlaps",
        action="store_true",
        help="Quarantine accounts that are in stock but also in the old extraction history",
    )
    legacy.add_argument("--json", type=Path, help="Also write the report as JSON")
    rec = sub.add_parser(
        "eldorado-record", help="Save redacted Eldorado responses (GET only) as test fixtures"
    )
    rec.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "eldorado-record":
        from app.connectors.eldorado.record import record, write_fixtures

        client_id = input("Eldorado client id: ").strip()
        client_secret = getpass.getpass("Eldorado client secret: ").strip()
        for path in write_fixtures(asyncio.run(record(client_id, client_secret)), args.out):
            print(f"wrote {path}")
        return

    if args.command == "migrate-legacy":
        ok = asyncio.run(
            migrate_legacy(
                args.store,
                args.db,
                dry_run=args.dry_run,
                quarantine_overlaps=args.quarantine_overlaps,
                json_out=args.json,
            )
        )
        sys.exit(0 if ok else 1)

    if args.command == "generate-key":
        print(generate_key())
        return
    password = getpass.getpass("Password (12+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    try:
        identity.hash_password(password)
    except ValueError as exc:
        sys.exit(str(exc))
    asyncio.run(create_owner(args.email, args.name, password))


if __name__ == "__main__":
    main()
