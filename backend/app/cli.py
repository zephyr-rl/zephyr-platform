"""Administrative commands.

python -m app.cli create-owner --email you@example.com --name "Owner"
python -m app.cli generate-key
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("create-owner", help="Create the owner account")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", default="Owner")
    sub.add_parser("generate-key", help="Print a new secret key for ZP_SECRET_KEYS")
    args = parser.parse_args(argv)

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
