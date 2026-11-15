"""Copy the old stores' data into ``<root>\\legacy`` for the importers and the daily comparison.

The old folders are only read: the JSON files are copied, and each SQLite database is copied
with SQLite's own backup (opened read-only), which gives a consistent copy even while the old
bots are writing to it. Folders named like backups are never opened.

SNAPSHOT_OLD.bat in the install folder runs:

    python -m app.deploy.legacy_snapshot --root C:\\ZephyrPlatform --source C:\\EldoradoBots
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

STORE_PREFIX = "Eldorado API - "
SKIP_DIRS = {"bin", "__pycache__", "_archives", "SAFETY_ACCOUNTS_BACKUP", "backups", "logs", ".git"}
MAX_DEPTH = 2


def copy_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_name(dst.name + ".part")
    partial.unlink(missing_ok=True)
    source = sqlite3.connect(
        "file:" + quote(src.resolve().as_posix(), safe="/:") + "?mode=ro", uri=True
    )
    try:
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    partial.replace(dst)


def _files(folder: Path, depth: int = 0) -> list[Path]:
    out: list[Path] = []
    for entry in sorted(folder.iterdir()):
        if entry.is_dir():
            skip = entry.name in SKIP_DIRS or "backup" in entry.name.lower()
            if not skip and depth < MAX_DEPTH:
                out += _files(entry, depth + 1)
        elif entry.suffix in {".json", ".sqlite3", ".sqlite", ".db"}:
            out.append(entry)
    return out


def snapshot_store(src: Path, dst: Path) -> list[str]:
    copied = []
    for path in _files(src):
        rel = path.relative_to(src)
        if path.suffix == ".json":
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst / rel)
        else:
            copy_sqlite(path, dst / rel)
        copied.append(rel.as_posix())
    return copied


def snapshot(source: Path, root: Path) -> dict[str, list[str]]:
    stores = sorted(p for p in source.iterdir() if p.is_dir() and p.name.startswith(STORE_PREFIX))
    return {store.name: snapshot_store(store, root / "legacy" / store.name) for store in stores}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.deploy.legacy_snapshot")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=Path(r"C:\EldoradoBots"))
    args = parser.parse_args(argv)
    if not args.source.is_dir():
        print(f"{args.source} does not exist.", file=sys.stderr)
        return 1
    result = snapshot(args.source, args.root.resolve())
    if not result:
        print(f"No '{STORE_PREFIX}*' folders in {args.source}.", file=sys.stderr)
        return 1
    for store, files in result.items():
        print(f"{store}: {len(files)} files -> {args.root / 'legacy' / store}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
