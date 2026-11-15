"""Build the Windows install kit: the backend, the built panel and the install scripts.

    cd frontend && npm ci && npm run build && cd ..
    python scripts/build_windows_kit.py --out dist/ZephyrPlatform-kit --zip

Kit layout:
    INSTALL.bat  UPDATE.bat  README.txt  DEPLOY-WINDOWS.md
    scripts\\     PowerShell scripts and the .bat files copied into the install folder
    app\\backend  app\\panel  app\\VERSION
    downloads\\   (optional) put postgresql-16.x-windows-x64.exe here to install without downloading
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEXT = {".bat", ".ps1", ".txt", ".md"}
BACKEND = ["app", "migrations", "alembic.ini", "pyproject.toml", "uv.lock"]


def crlf(path: Path) -> None:
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    path.write_bytes(data)


def version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    return f"Zephyr Platform {sha} built {datetime.now(UTC):%Y-%m-%d %H:%M} UTC"


def build(out: Path) -> Path:
    panel = REPO / "frontend" / "dist"
    if not (panel / "index.html").is_file():
        sys.exit("frontend/dist is missing: run npm run build in frontend first.")
    if out.exists():
        shutil.rmtree(out)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache")
    for name in BACKEND:
        src = REPO / "backend" / name
        dst = out / "app" / "backend" / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=ignore)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    shutil.copytree(panel, out / "app" / "panel")
    (out / "app" / "VERSION").write_text(version() + "\n", encoding="ascii")
    windows = REPO / "deploy" / "windows"
    shutil.copytree(windows, out / "scripts", ignore=shutil.ignore_patterns("README.txt"))
    for top in ("INSTALL.bat", "UPDATE.bat"):
        shutil.move(out / "scripts" / top, out / top)
    shutil.copy2(windows / "README.txt", out / "README.txt")
    shutil.copy2(REPO / "docs" / "DEPLOY-WINDOWS.md", out / "DEPLOY-WINDOWS.md")
    (out / "downloads").mkdir()
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in TEXT and "panel" not in path.parts:
            crlf(path)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=REPO / "dist" / "ZephyrPlatform-kit")
    parser.add_argument("--zip", action="store_true", help="Also make <out>.zip")
    args = parser.parse_args()
    out = build(args.out.resolve())
    print(f"kit: {out}")
    if args.zip:
        archive = shutil.make_archive(str(out), "zip", root_dir=out.parent, base_dir=out.name)
        print(f"zip: {archive}")


if __name__ == "__main__":
    main()
