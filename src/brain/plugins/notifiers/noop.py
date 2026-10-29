from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("brain.plugins.notifier.noop")


class NoopNotifier:
    def __init__(self, **_ignored) -> None:
        pass

    async def post_media(self, media_ids: list[str], **_kwargs) -> dict[str, Any]:
        return {}

    async def post_blocks(
        self, *, channel: str, text: str, blocks: list[dict],
        thread_ts: str | None = None, metadata: dict | None = None,
    ) -> str | None:
        log.info("[noop] block message for %s not posted: %s", channel, text[:80])
        return None

    async def add_reaction(self, *, channel: str, ts: str, name: str) -> None:
        return None

    async def remove_reaction(self, *, channel: str, ts: str, name: str) -> None:
        return None

    async def publish_home(self, *, user: str, view: dict) -> bool:
        return False

    async def upload_private_file(self, *, path, title: str) -> str | None:
        return None

    async def send(
        self,
        text: str,
        *,
        title: str = "",
        urgency: str = "info",
        finding_id: str | None = None,
        event_type: str | None = None,
        area: str | None = None,
        agent: str | None = None,
        quote_timestamp: int | str | None = None,
        idempotency_key: str | None = None,
        plain_text: bool = False,
        proposal_action_ids: list[str] | None = None,
        require_explicit_area: bool = False,
        media_ids: list[str] | None = None,
        place: str | None = None,
    ) -> dict[str, Any]:
        area_note = f" (area={area})" if area else ""
        log.info(
            "noop-notifier%s: urgency=%s title=%s chars=%d",
            area_note,
            urgency,
            bool(title),
            len(text),
        )
        return {"status": "noop", "id": None}
