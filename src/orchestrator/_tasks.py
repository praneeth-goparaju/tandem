from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

# Strong refs to in-flight tasks — without this, the event loop only keeps a
# weak reference and a task may be GC'd before it finishes.
_background_tasks: set[asyncio.Task] = set()


def spawn_tracked(
    coro: Coroutine[Any, Any, Any],
    label: str,
    logger: logging.Logger,
) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("%s crashed: %s", label, exc, exc_info=exc)

    task.add_done_callback(_done)
    return task
