"""Stop a long-running process when a file appears (Windows has no SIGTERM for services).

The Windows supervisor and the update script create the file; the worker and the scheduler
finish what they are doing and exit.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable


async def watch(path: str | None, stop: Callable[[], None], every: float = 1.0) -> None:
    if not path:
        return
    while True:
        if os.path.exists(path):  # noqa: ASYNC240 (a cheap stat once a second)
            stop()
            return
        await asyncio.sleep(every)
