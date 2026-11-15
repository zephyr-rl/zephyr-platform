"""The Windows install's Python parts: .env, database creation, backups, the supervisor."""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.engine import make_url

from app.core import stopfile
from app.deploy import backup, envfile, setup, supervisor
from tests.conftest import TEST_DB_URL

ADMIN = make_url(TEST_DB_URL).set(drivername="postgresql", database="postgres")
ADMIN_DSN = ADMIN.render_as_string(hide_password=False)


def test_init_env_creates_keys_once(tmp_path: Path) -> None:
    first = setup.init_env(tmp_path, port=8600, pg_port=5433)
    assert first["ZP_DATABASE_URL"].startswith("postgresql+asyncpg://zephyr:")
    assert "@127.0.0.1:5433/zephyr" in first["ZP_DATABASE_URL"]
    assert (
        len(first["ZP_SECRET_KEYS"]) >= 43
        and first["ZP_SECRET_KEYS"] != first["ZP_FINGERPRINT_KEY"]
    )
    # Running the installer again never changes the keys or the passwords.
    again = setup.init_env(tmp_path, port=9999, pg_port=6000)
    assert again == first
    assert envfile.read(tmp_path / ".env") == first
    assert (tmp_path / ".env").read_text().startswith("# Zephyr Platform settings")


def test_envfile_reads_quotes_and_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('# comment\nA=1\nB="two words"\n\nbroken line\nC = x=y\n', encoding="utf-8")
    assert envfile.read(path) == {"A": "1", "B": "two words", "C": "x=y"}


async def test_create_database_is_repeatable() -> None:
    password = envfile.new_password()
    values = {
        "ZP_DATABASE_URL": f"postgresql+asyncpg://zp_deploy_test:{password}@x:1/zp_deploy_test"
    }
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute("drop database if exists zp_deploy_test")
        await admin.execute("drop role if exists zp_deploy_test")
        assert await setup.create_database(values, admin_dsn=ADMIN_DSN) == [
            "role zp_deploy_test created",
            "database zp_deploy_test created",
        ]
        values["ZP_DATABASE_URL"] = values["ZP_DATABASE_URL"].replace(
            password, envfile.new_password()
        )
        assert await setup.create_database(values, admin_dsn=ADMIN_DSN) == [
            "role zp_deploy_test updated"
        ]
        owner = await admin.fetchval(
            "select pg_get_userbyid(datdba) from pg_database where datname = 'zp_deploy_test'"
        )
        assert owner == "zp_deploy_test"
    finally:
        await admin.execute("drop database if exists zp_deploy_test")
        await admin.execute("drop role if exists zp_deploy_test")
        await admin.close()


def test_password_literal_refuses_quotes() -> None:
    with pytest.raises(ValueError):
        setup._literal("abc'; drop role postgres; --xxxxxxx")


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_backup_dump_and_prune(tmp_path: Path) -> None:
    envfile.write(tmp_path / ".env", {"ZP_DATABASE_URL": TEST_DB_URL})
    target = backup.dump(tmp_path, now=datetime(2026, 11, 15, 4, 17))
    assert target.name == "zephyr-20261115-0417.dump" and target.stat().st_size > 0
    assert target.read_bytes()[:5] == b"PGDMP"  # pg_dump custom format
    old = tmp_path / "backups" / "zephyr-20261001-0417.dump"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 20 * 86400,) * 2)
    leftover = tmp_path / "backups" / "zephyr-20261114-0417.dump.tmp"
    leftover.write_bytes(b"x")
    assert backup.prune(tmp_path / "backups") == [old]
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == [target.name]


def test_backup_failure_leaves_no_partial_file(tmp_path: Path) -> None:
    envfile.write(
        tmp_path / ".env", {"ZP_DATABASE_URL": "postgresql+asyncpg://nobody:pw@127.0.0.1:1/none"}
    )
    fake = tmp_path / "pg_dump"
    fake.write_text("#!/bin/sh\necho 'connection refused' >&2\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(RuntimeError, match="connection refused"):
        backup.dump(tmp_path, pg_dump=str(fake))
    assert list((tmp_path / "backups").iterdir()) == []


def test_supervisor_environment_and_commands(tmp_path: Path) -> None:
    envfile.write(tmp_path / ".env", {"ZP_PORT": "8611", "ZP_WORKERS": "3", "ZP_SECRET_KEYS": "k"})
    paths = supervisor.Paths(tmp_path)
    env = supervisor.environment(paths)
    assert env["ZP_SECRET_KEYS"] == "k" and env["ZP_ENV"] == "production"
    assert env["ZP_STOP_FILE"] == str(tmp_path / "run" / "stop")
    assert env["ZP_STATIC_DIR"] == str(tmp_path / "app" / "panel")
    cmds = supervisor.commands(env)
    assert sorted(cmds) == ["api", "scheduler", "worker-1", "worker-2", "worker-3"]
    api = cmds["api"]
    # Only reachable from the machine itself; forwarded headers trusted only from it.
    assert api[api.index("--host") + 1] == "127.0.0.1" and api[api.index("--port") + 1] == "8611"
    assert api[api.index("--forwarded-allow-ips") + 1] == "127.0.0.1"


def _fake_backend(root: Path) -> None:
    backend = root / "app" / "backend"
    (backend / "alembic").mkdir(parents=True)
    # "python -m alembic upgrade head" in the backend folder: a stand-in that succeeds.
    (backend / "alembic" / "__init__.py").write_text("")
    (backend / "alembic" / "__main__.py").write_text("print('upgraded')\n")


def test_supervisor_restarts_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_backend(tmp_path)
    (tmp_path / "logs").mkdir()
    marker = tmp_path / "starts.txt"
    script = (
        "import os,sys,time\n"
        f"open({str(marker)!r},'a').write(sys.argv[1]+'\\n')\n"
        "if sys.argv[1]=='flaky': sys.exit(3)\n"
        "while not os.path.exists(os.environ['ZP_STOP_FILE']): time.sleep(0.1)\n"
        "print('bye')\n"
    )
    monkeypatch.setattr(
        supervisor,
        "commands",
        lambda env: {
            "steady": [sys.executable, "-c", script, "steady"],
            "flaky": [sys.executable, "-c", script, "flaky"],
        },
    )
    paths = supervisor.Paths(tmp_path)
    sup = supervisor.Supervisor(paths)
    sup.migrate()
    assert "upgraded" in (tmp_path / "logs" / "migrations.log").read_text()

    now = time.monotonic()
    for child in sup.children:
        sup.check(child, now)
    time.sleep(1.0)
    flaky = next(c for c in sup.children if c.name == "flaky")
    sup.check(flaky, time.monotonic())  # it exited: wait 1 s before starting it again
    assert flaky.failures == 1 and flaky.process is None
    sup.check(flaky, flaky.next_start)
    time.sleep(1.0)
    sup.check(flaky, time.monotonic())
    assert flaky.failures == 2 and flaky.next_start - time.monotonic() > 1.0  # 2 s now

    (paths.run).mkdir(exist_ok=True)
    paths.stop_file.touch()
    sup.stop_all()
    steady = next(c for c in sup.children if c.name == "steady")
    assert steady.process is not None and steady.process.returncode == 0  # it stopped by itself
    time.sleep(0.3)
    assert "bye" in (tmp_path / "logs" / "steady.log").read_text()
    assert marker.read_text().split().count("flaky") == 2


def test_supervisor_does_not_start_while_stop_file_exists(tmp_path: Path) -> None:
    paths = supervisor.Paths(tmp_path)
    paths.run.mkdir(parents=True)
    paths.stop_file.touch()
    assert supervisor.Supervisor(paths).run() == 0


def test_single_instance(tmp_path: Path) -> None:
    lock = tmp_path / "run" / "supervisor.lock"
    with supervisor.InstanceLock(lock):
        assert (tmp_path / "run" / "supervisor.pid").read_text() == str(os.getpid())
        with pytest.raises(supervisor.AlreadyRunning), supervisor.InstanceLock(lock):
            pass
    assert not (tmp_path / "run" / "supervisor.pid").exists()


async def test_stop_file_watch(tmp_path: Path) -> None:
    calls: list[int] = []
    target = tmp_path / "stop"
    import asyncio

    task = asyncio.create_task(stopfile.watch(str(target), lambda: calls.append(1), every=0.05))
    await asyncio.sleep(0.15)
    assert calls == []
    target.touch()
    await asyncio.wait_for(task, 1)
    assert calls == [1]
    await stopfile.watch(None, lambda: calls.append(2))  # no file configured: returns at once
    assert calls == [1]


def test_legacy_snapshot_copies_consistently_and_skips_backups(tmp_path: Path) -> None:
    import json
    import sqlite3

    from app.deploy import legacy_snapshot

    source = tmp_path / "EldoradoBots"
    store = source / "Eldorado API - ZephyrStore"
    (store / "account_manager").mkdir(parents=True)
    (store / "bin").mkdir()
    (store / "bin" / "x.json").write_text("{}")
    (store / "account_backups").mkdir()
    (store / "account_backups" / "old.sqlite3").write_text("not touched")
    (source / "SAFETY_ACCOUNTS_BACKUP").mkdir()
    (source / "price-sniper").mkdir()
    (store / "config.json").write_text(json.dumps({"port": 5000}))
    (store / "item_delivery_logs.json").write_text("[]")
    db = store / "account_manager" / "accounts.sqlite3"
    live = sqlite3.connect(db)
    live.execute("pragma journal_mode=wal")
    live.execute("create table accounts (id integer primary key, login text)")
    live.executemany("insert into accounts (login) values (?)", [("a",), ("b",)])
    live.commit()
    live.execute("insert into accounts (login) values ('uncommitted')")  # the bot is mid-write

    root = tmp_path / "ZephyrPlatform"
    result = legacy_snapshot.snapshot(source, root)
    live.rollback()
    live.close()

    assert result == {
        "Eldorado API - ZephyrStore": [
            "account_manager/accounts.sqlite3",
            "config.json",
            "item_delivery_logs.json",
        ]
    }
    copy = sqlite3.connect(
        root / "legacy" / "Eldorado API - ZephyrStore" / "account_manager" / "accounts.sqlite3"
    )
    assert [r[0] for r in copy.execute("select login from accounts order by id")] == ["a", "b"]
    copy.close()
    assert (store / "account_backups" / "old.sqlite3").read_text() == "not touched"
    assert not (root / "legacy" / "Eldorado API - ZephyrStore" / "bin").exists()
