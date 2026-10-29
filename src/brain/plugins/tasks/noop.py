from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("brain.plugins.tasks.noop")


class NoopTaskManager:
    async def add(self, title: str, **kwargs: Any) -> dict[str, Any]:
        log.info("noop-tasks add: title_chars=%d fields=%s", len(title), sorted(kwargs))
        return {"id": "noop", "url": None}

    async def list_pending(self, *, project: str | None = None) -> list[dict[str, Any]]:
        return []

    async def list_completed(
        self, *, since_hours: int = 24, limit: int = 50
    ) -> list[dict[str, Any]]:
        return []
