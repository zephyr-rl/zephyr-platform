"""Run an administrative command with the install's settings (zephyr.bat calls this).

zephyr.bat compare-shadow --store zephyrstore ^
    --folder "C:\\EldoradoBots\\Eldorado API - ZephyrStore"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.deploy.supervisor import Paths, environment


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] != "--root":
        sys.exit("usage: python -m app.deploy.run --root <folder> <command> [options]")
    paths = Paths(Path(args[1]).resolve())
    os.environ.update(environment(paths))  # relative paths stay relative to where you are
    from app.cli import main as cli

    cli(args[2:])


if __name__ == "__main__":
    main()
