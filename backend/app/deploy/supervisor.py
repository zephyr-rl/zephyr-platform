"""Keeps the platform running on Windows: the API, the workers and the scheduler.

Started by the scheduled task "Zephyr Platform" (at start-up, and again every 5 minutes in case
it stopped; a second copy exits at once). It

* reads ``<root>\\.env``;
* brings the database up to date (``alembic upgrade head``), retrying until the database answers;
* starts each process, writes its output to ``<root>\\logs\\<name>.log`` (rotated) and starts it
  again if it stops, waiting longer each time it fails quickly;
* stops everything when ``<root>\\run\\stop`` exists (the update script creates it): the
  workers and the scheduler finish their current job first.

    python -m app.deploy.supervisor --root C:\\ZephyrPlatform
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from app.deploy import envfile

log = logging.getLogger("supervisor")

LOG_BYTES = 20 * 1024 * 1024
LOG_FILES = 5
STABLE_SECONDS = 300  # a process that ran this long and then stopped is restarted at once
MAX_BACKOFF = 60.0
GRACE_SECONDS = 40  # workers finish their current job; then they are stopped


@dataclass
class Paths:
    root: Path

    @property
    def backend(self) -> Path:
        return self.root / "app" / "backend"

    @property
    def panel(self) -> Path:
        return self.root / "app" / "panel"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def stop_file(self) -> Path:
        return self.run / "stop"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def backups(self) -> Path:
        return self.root / "backups"


def environment(paths: Paths) -> dict[str, str]:
    """The environment every process gets: the .env file plus the install's own folders."""
    env = dict(os.environ)
    env.update(envfile.read(paths.env_file))
    env.setdefault("ZP_ENV", "production")
    env["ZP_STATIC_DIR"] = str(paths.panel)
    env["ZP_BACKUP_DIR"] = str(paths.backups)
    env["ZP_STOP_FILE"] = str(paths.stop_file)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def commands(env: dict[str, str]) -> dict[str, list[str]]:
    py = sys.executable
    port = env.get("ZP_PORT", "8600")
    workers = max(1, min(8, int(env.get("ZP_WORKERS", "2"))))
    out = {
        # Only this machine can reach it: the browser on the server, or the Cloudflare tunnel.
        "api": [
            py,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            port,
            "--proxy-headers",
            "--forwarded-allow-ips",
            "127.0.0.1",
            "--no-server-header",
        ],
        "scheduler": [py, "-m", "app.workers.scheduler"],
    }
    for n in range(1, workers + 1):
        out[f"worker-{n}"] = [py, "-m", "app.workers.worker"]
    return out


def file_logger(paths: Paths, name: str) -> logging.Logger:
    logger = logging.getLogger(f"proc.{name}")
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            paths.logs / f"{name}.log", maxBytes=LOG_BYTES, backupCount=LOG_FILES, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _pump(stream: IO[bytes], logger: logging.Logger) -> None:
    for raw in iter(stream.readline, b""):
        logger.info(raw.decode("utf-8", errors="replace").rstrip())
    stream.close()


@dataclass
class Child:
    name: str
    argv: list[str]
    process: subprocess.Popen[bytes] | None = None
    started: float = 0.0
    failures: int = 0
    next_start: float = 0.0
    pump: threading.Thread | None = field(default=None, repr=False)

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class Supervisor:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.env = environment(paths)
        self.children = [Child(name, argv) for name, argv in commands(self.env).items()]

    # -- one process ----------------------------------------------------------------------------

    def start(self, child: Child) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child.process = subprocess.Popen(  # noqa: S603 (our own interpreter and module)
            child.argv,
            cwd=self.paths.backend,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        child.started = time.monotonic()
        assert child.process.stdout is not None
        child.pump = threading.Thread(
            target=_pump,
            args=(child.process.stdout, file_logger(self.paths, child.name)),
            daemon=True,
        )
        child.pump.start()
        log.info("started %s (pid %s)", child.name, child.process.pid)

    def check(self, child: Child, now: float) -> None:
        if child.running():
            return
        if child.process is not None:
            code = child.process.returncode
            ran = now - child.started
            child.failures = 0 if ran >= STABLE_SECONDS else child.failures + 1
            delay = 0.0 if child.failures == 0 else min(MAX_BACKOFF, 2.0 ** (child.failures - 1))
            child.next_start = now + delay
            log.warning(
                "%s stopped (exit %s after %.0fs); again in %.0fs", child.name, code, ran, delay
            )
            child.process = None
        if now >= child.next_start:
            self.start(child)

    # -- the whole set --------------------------------------------------------------------------

    def migrate(self) -> bool:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=self.paths.backend,
            env=self.env,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        out = file_logger(self.paths, "migrations")
        for line in (result.stdout + result.stderr).splitlines():
            out.info(line)
        if result.returncode != 0:
            log.error(
                "database update failed (exit %s): see logs\\migrations.log", result.returncode
            )
        return result.returncode == 0

    def stop_all(self) -> None:
        # The workers and the scheduler see the stop file themselves and finish their job.
        deadline = time.monotonic() + GRACE_SECONDS
        api = [c for c in self.children if c.name == "api"]
        for child in api:
            if child.running() and child.process is not None:
                child.process.terminate()
        while time.monotonic() < deadline and any(c.running() for c in self.children):
            time.sleep(0.5)
        for child in self.children:
            if child.running() and child.process is not None:
                log.warning("%s did not stop in time; ending it", child.name)
                child.process.kill()
        for child in self.children:
            if child.process is not None:
                child.process.wait(10)
        log.info("everything stopped")

    def run(self, *, poll: float = 1.0, max_rounds: int | None = None) -> int:
        if self.paths.stop_file.exists():
            log.info("the stop file exists (an update is running): not starting")
            return 0
        while not self.migrate():
            for _ in range(60):
                if self.paths.stop_file.exists():
                    return 0
                time.sleep(1)
        rounds = 0
        try:
            while not self.paths.stop_file.exists():
                now = time.monotonic()
                for child in self.children:
                    self.check(child, now)
                rounds += 1
                if max_rounds is not None and rounds >= max_rounds:
                    break
                time.sleep(poll)
        finally:
            self.stop_all()
        return 0


# -- single instance ------------------------------------------------------------------------------


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    """Held while the supervisor runs; the scheduled task starts it every 5 minutes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: IO[str] | None = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning from None
        self.handle = handle
        # Informative only (the lock file itself cannot be read by others on Windows).
        self.path.with_suffix(".pid").write_text(str(os.getpid()), encoding="ascii")
        return self

    def __exit__(self, *exc: object) -> None:
        if self.handle is not None:
            self.handle.close()
        self.path.with_suffix(".pid").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.deploy.supervisor")
    parser.add_argument("--root", type=Path, required=True, help="The install folder")
    args = parser.parse_args(argv)
    paths = Paths(args.root.resolve())
    paths.logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        paths.logs / "supervisor.log", maxBytes=LOG_BYTES, backupCount=LOG_FILES, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        with InstanceLock(paths.run / "supervisor.lock"):
            log.info("supervisor starting in %s", paths.root)
            return Supervisor(paths).run()
    except AlreadyRunning:
        return 0


if __name__ == "__main__":
    sys.exit(main())
