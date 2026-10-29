from __future__ import annotations

import asyncio
import contextlib
import uuid
from unittest.mock import MagicMock, patch

import vobject

from src.brain.actions.handlers import create_calendar_event, create_contact

VICTIM = "existing-victim-uid"
EXPECTED = uuid.uuid5(uuid.NAMESPACE_URL, "brain-action:a1")


def test_create_calendar_event_ignores_a_payload_uid() -> None:
    saved: list[str] = []
    calendar = MagicMock()
    calendar.name = "iCloud"
    calendar.save_event = lambda ics: saved.append(ics) or MagicMock(url="https://caldav.icloud.com/e.ics")
    client = MagicMock()
    client.principal.return_value.calendars.return_value = [calendar]

    with patch("src.brain.mcp.tools._icloud.caldav_client", return_value=client):
        result = asyncio.run(create_calendar_event.execute({
            "summary": "Meeting", "start": "2026-05-12T14:00:00Z", "end": "2026-05-12T15:00:00Z",
            "uid": VICTIM, "_action_id": "a1",
        }))

    assert result["ok"] is True
    assert vobject.readOne(saved[0]).vevent.uid.value == f"brain-{EXPECTED}"


def test_create_contact_ignores_a_payload_uid() -> None:
    client = MagicMock()
    client.put.return_value = MagicMock(status_code=201, text="")

    @contextlib.contextmanager
    def factory():
        yield client

    with patch("src.brain.mcp.tools._icloud.carddav_http_client", factory), patch(
        "src.brain.mcp.tools._icloud.carddav_addressbook_url",
        return_value="https://contacts.icloud.com/card/",
    ):
        result = asyncio.run(create_contact.execute({
            "full_name": "Jane Doe", "uid": VICTIM, "_action_id": "a1",
        }))

    assert result["ok"] is True
    target = client.put.call_args.args[0]
    assert target == f"https://contacts.icloud.com/card/{str(EXPECTED).upper()}.vcf"
    assert vobject.readOne(client.put.call_args.kwargs["content"]).uid.value == str(EXPECTED).upper()
