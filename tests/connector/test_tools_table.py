from __future__ import annotations

import asyncio
import hashlib
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from src.brain.connector import tools, tools_icloud
from src.brain.connector.config import ConnectorConfig, Integrations
from src.brain.connector.integrations import apple, build_tools
from src.brain.connector.layout import VaultLayout
from src.brain.connector.tools import Call, ToolEnv, run_tool, visible
from src.brain.connector.vault import write
from src.brain.connector.vault.undo_store import UndoStore
from src.gateway.validation import validate_schema

FIXTURE = Path(__file__).parent.parent / "fixtures" / "connector_vault"
ALL_ON = ConnectorConfig(
    vault_path=Path("/no/such/vault"),
    data_dir=Path("/no/such/data"),
    public_url="https://vault.example.com",
    allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
    integrations=Integrations(reminders=True, calendar=True, mail=True, contacts=True),
)
TOOLS = build_tools(ALL_ON)

ALL_TOOLS = {
    "vault_search",
    "vault_read",
    "vault_list",
    "vault_append",
    "vault_create",
    "vault_edit",
    "vault_edit_confirm",
    "vault_undo",
    "open_item_add",
    "reminders_list",
    "reminder_create",
    "reminder_update",
    "reminder_complete",
    "calendar_list",
    "calendar_create",
    "calendar_update",
    "mail_search",
    "mail_read",
    "mail_archive",
    "contacts_search",
}
LIMITED_TOOLS = {
    "vault_search",
    "vault_read",
    "vault_list",
    "vault_append",
    "vault_create",
    "open_item_add",
    "reminders_list",
    "reminder_create",
    "calendar_list",
    "mail_search",
    "mail_read",
    "contacts_search",
}
READ_TOOLS = {
    "vault_search",
    "vault_read",
    "vault_list",
    "reminders_list",
    "calendar_list",
    "mail_search",
    "mail_read",
    "contacts_search",
}
NEEDS_APPROVAL = {
    "vault_edit_confirm",
    "vault_undo",
    "reminder_update",
    "reminder_complete",
    "calendar_create",
    "calendar_update",
    "mail_archive",
}


def _by_name() -> dict[str, tools.ToolDef]:
    return {tool.name: tool for tool in TOOLS}


@pytest.fixture
def env(tmp_path: Path) -> ToolEnv:
    vault = tmp_path / "Vault"
    shutil.copytree(FIXTURE, vault)
    return ToolEnv(
        vault=vault,
        undo=UndoStore(tmp_path / "undo.db"),
        icloud=lambda name, arguments: f"{name} answered",
    )


def _run(env: ToolEnv, name: str, args: dict[str, Any], mail: bool = False) -> str:
    return asyncio.run(run_tool(_by_name()[name], env, args, Call(mail_read_recently=mail)))


def test_limited_has_exactly_the_spec_tools() -> None:
    assert {tool.name for tool in TOOLS if tool.limited} == LIMITED_TOOLS


def test_full_has_all_twenty() -> None:
    assert len(TOOLS) == 20
    assert {tool.name for tool in TOOLS if tool.full} == ALL_TOOLS


def test_annotations() -> None:
    by_name = _by_name()
    for name in ("vault_undo", "reminder_complete", "mail_archive"):
        assert by_name[name].destructive
    for tool in TOOLS:
        assert tool.read_only == (tool.name in READ_TOOLS), tool.name
        assert tool.destructive == (tool.name in NEEDS_APPROVAL), tool.name


def test_no_tool_named_send_or_delete() -> None:
    for tool in TOOLS:
        assert "send" not in tool.name
        assert "delete" not in tool.name
        assert "move" not in tool.name


def test_schemas_are_closed_objects_with_plain_descriptions() -> None:
    for tool in TOOLS:
        validate_schema(tool.schema)
        assert tool.schema["type"] == "object"
        assert tool.schema["additionalProperties"] is False
        assert "never" in tool.description.casefold(), tool.name
        assert "—" not in tool.description


def test_tool_errors_are_text(env: ToolEnv) -> None:
    text = _run(env, "vault_append", {"path": "CLAUDE.md", "text": "planted"})

    assert text.startswith("ERROR: ")
    assert "protected" in text


def test_errors_never_show_the_vault_folder(env: ToolEnv) -> None:
    for name, args in (
        ("vault_read", {"path": "../outside.md"}),
        ("vault_read", {"path": "Notes/Missing.md"}),
        ("vault_list", {"path": "No Such Folder"}),
        ("vault_create", {"path": "Notes/Appointments.md", "text": "x"}),
    ):
        text = _run(env, name, args)
        assert text.startswith("ERROR: "), (name, text)
        assert str(env.vault) not in text
        assert str(env.vault.resolve()) not in text


def test_bad_arguments_are_text(env: ToolEnv) -> None:
    text = _run(env, "vault_read", {"path": 3})
    assert text.startswith("ERROR: ")

    text = _run(env, "vault_read", {"path": "Household.md", "planted": True})
    assert text.startswith("ERROR: ")


def test_unexpected_exception_is_text(env: ToolEnv) -> None:
    def broken(name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("bridge fell over")

    env.icloud = broken
    text = _run(env, "contacts_search", {"query": "Anna"})

    assert text == "ERROR: RuntimeError: bridge fell over"


def test_icloud_tools_go_through_the_bridge_function(env: ToolEnv) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake(name: str, arguments: dict[str, Any]) -> str:
        calls.append((name, arguments))
        return "answer"

    env.icloud = fake
    assert _run(env, "mail_read", {"uid": "42"}) == "answer"
    assert _run(env, "calendar_list", {"date_from": "2026-10-22"}) == "answer"
    assert _run(env, "reminders_list", {}) == "answer"
    assert _run(env, "mail_search", {"query": "school"}) == "answer"
    assert _run(env, "contacts_search", {"query": "Anna"}) == "answer"

    assert calls == [
        ("mail_get_message", {"uid": "42"}),
        ("calendar_list_events", {"date_from": "2026-10-22"}),
        ("reminders_list", {}),
        ("mail_search", {"query": "school"}),
        ("contacts_search", {"query": "Anna"}),
    ]


def test_icloud_tools_use_the_bridge_when_the_env_names_none(
    env: ToolEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(apple, "icloud_bridge", lambda name, arguments: calls.append(name) or "ok")

    assert _run(replace(env, icloud=None), "contacts_search", {"query": "Anna"}) == "ok"
    assert calls == ["contacts_search"]


def test_reminder_create_calls_the_handler(env: ToolEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": True, "result": "created reminder"}

    monkeypatch.setattr(apple.create_reminder, "execute", fake_execute)
    text = _run(
        env,
        "reminder_create",
        {"title": "Pack the school bag", "due": "2026-10-23T07:00", "list_name": "List A"},
    )

    assert text == "created reminder"
    assert seen == [
        {"title": "Pack the school bag", "due": "2026-10-23T07:00", "list_name": "List A"}
    ]


def test_calendar_create_defaults_to_a_60_minute_alert(
    env: ToolEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": False, "result": "no matching calendar found"}

    monkeypatch.setattr(apple.create_calendar_event, "execute", fake_execute)
    text = _run(
        env,
        "calendar_create",
        {"summary": "Dentist", "start": "2026-10-23T09:00", "end": "2026-10-23T10:00"},
    )

    assert text == "ERROR: no matching calendar found"
    assert seen[0]["alert_minutes"] == 60


def test_mail_archive_calls_archive_message(env: ToolEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, str]] = []

    async def fake_archive(provider: str, uid: str, mailbox: str = "INBOX") -> str:
        seen.append((provider, uid, mailbox))
        return "Archive"

    monkeypatch.setattr(apple.mail_ops, "archive_message", fake_archive)
    text = _run(env, "mail_archive", {"uid": "42"})

    assert seen == [("icloud", "42", "INBOX")]
    assert "42" in text and not text.startswith("ERROR")


def test_vault_writes_run_one_at_a_time(env: ToolEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    running = 0
    most = 0
    real_append = tools.write.append

    def slow_append(*args: Any, **kwargs: Any) -> Any:
        nonlocal running, most
        running += 1
        most = max(most, running)
        try:
            import time

            time.sleep(0.05)
            return real_append(*args, **kwargs)
        finally:
            running -= 1

    monkeypatch.setattr(tools.write, "append", slow_append)

    async def three() -> list[str]:
        tool = _by_name()["vault_append"]
        return await asyncio.gather(
            *(
                run_tool(tool, env, {"path": "Household.md", "text": f"- item {n}"}, Call())
                for n in range(3)
            )
        )

    results = asyncio.run(three())

    assert most == 1
    assert all(not text.startswith("ERROR") for text in results), results


def test_held_edit_is_legible(env: ToolEnv) -> None:
    read = _run(env, "vault_read", {"path": "Notes/Appointments.md"})
    expected_hash = read.split("hash: ", 1)[1].split("\n", 1)[0]

    text = _run(
        env,
        "vault_edit",
        {
            "path": "Notes/Appointments.md",
            "old": "appointment two",
            "new": "appointment two moved",
            "expected_hash": expected_hash,
            "reason": "move the second appointment",
        },
        mail=True,
    )

    assert "held" in text
    assert "Notes/Appointments.md" in text
    assert "appointment two" in text
    assert "appointment\\u2028two moved" in text
    assert " " not in text
    assert "Call vault_edit_confirm with this id to run it." in text


@pytest.fixture
def custom_env(env: ToolEnv) -> ToolEnv:
    vault = env.vault
    (vault / "Journal").mkdir()
    (vault / "Journal" / "Action Queue.md").write_text("queue\n", encoding="utf-8")
    (vault / "Open").mkdir()
    (vault / "Open" / "A.md").write_text(
        "---\ntype: action\nstatus: next\n---\n# Send the form\n", encoding="utf-8"
    )
    return replace(env, layout=VaultLayout(daily_notes_dir="Journal", actions_dir="Open"))


def test_owner_only_path_follows_the_layout_through_the_tools(custom_env: ToolEnv) -> None:
    env = custom_env
    refused = _run(env, "vault_append", {"path": "Journal/Action Queue.md", "text": "- x"})
    assert "owner-only" in refused
    assert (env.vault / "Journal" / "Action Queue.md").read_text(encoding="utf-8") == "queue\n"

    refused = _run(env, "vault_create", {"path": "Journal/Action Queue.md", "text": "x"})
    assert "owner-only" in refused

    queue_hash = hashlib.sha256((env.vault / "Journal" / "Action Queue.md").read_bytes())
    refused = _run(
        env,
        "vault_edit",
        {
            "path": "Journal/Action Queue.md",
            "old": "queue",
            "new": "line",
            "expected_hash": queue_hash.hexdigest(),
            "reason": "tidy the queue note",
        },
    )
    assert "owner-only" in refused

    held_id = env.undo.hold("Journal/Action Queue.md", "queue", "line", "0" * 64, "tidy up")
    refused = _run(env, "vault_edit_confirm", {"held_id": held_id})
    assert "owner-only" in refused
    assert (env.vault / "Journal" / "Action Queue.md").read_text(encoding="utf-8") == "queue\n"

    edit_id = env.undo.record("Journal/Action Queue.md", "queue", "line", "0" * 64, 0)
    refused = _run(env, "vault_undo", {"edit_id": edit_id})
    assert "owner-only" in refused

    allowed = _run(env, "vault_append", {"path": "01 - Daily Notes/Action Queue.md", "text": "- x"})
    assert "owner-only" not in allowed


def test_status_change_in_the_actions_dir_runs_through_the_tools(custom_env: ToolEnv) -> None:
    env = custom_env
    note = env.vault / "Open" / "A.md"
    args = {
        "old": "status: next",
        "new": "status: done",
        "expected_hash": hashlib.sha256(note.read_bytes()).hexdigest(),
        "reason": "mark the form as sent",
    }

    text = _run(env, "vault_edit", {"path": "Open/A.md", **args})

    assert text.startswith("Done.")
    assert "status: done" in note.read_text(encoding="utf-8")

    stale = env.vault / "Actions"
    stale.mkdir()
    other = stale / "A.md"
    other.write_text("---\ntype: action\nstatus: next\n---\n# Other\n", encoding="utf-8")
    args["expected_hash"] = hashlib.sha256(other.read_bytes()).hexdigest()

    held = _run(env, "vault_edit", {"path": "Actions/A.md", **args})

    assert "held" in held
    assert "status: next" in other.read_text(encoding="utf-8")


def test_old_module_still_names_the_apple_helpers() -> None:
    for name in (
        "ICloudCall",
        "icloud_bridge",
        "brain_schema",
        "local_time",
        "reminder_create",
        "reminder_update",
        "reminder_complete",
        "calendar_create",
        "calendar_update",
        "mail_archive",
        "ICLOUD_ONLY",
        "DEFAULT_ALERT_MINUTES",
        "OTHERS_REFUSED",
    ):
        assert getattr(tools_icloud, name) is getattr(apple, name), name


def test_visible_shows_invisible_characters() -> None:
    assert visible("a\tb\r\nc d​e") == "a\\tb\\r\\n\nc\\u00a0d\\u200be"


class _FakeBackend:
    payloads: list[dict[str, Any]] = []
    answer: Any = {"is_error": False, "content": [{"type": "text", "text": "two events"}]}

    def __init__(self, settings: Any) -> None:
        pass

    def invoke(self, operation: str, payload: dict[str, Any] | None = None) -> Any:
        assert operation == "call"
        assert payload is not None
        self.payloads.append(payload)
        return self.answer


def test_icloud_bridge_runs_one_tool_under_the_read_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.gateway.profiles import load_profile

    _FakeBackend.payloads = []
    monkeypatch.setattr(apple, "CommandBackend", _FakeBackend)

    assert apple.icloud_bridge("calendar_list_events", {"limit": 2}) == "two events"
    assert _FakeBackend.payloads == [
        {
            "name": "calendar_list_events",
            "arguments": {"limit": 2},
            "allowed": ["calendar_list_events"],
            "caller_context": load_profile({"BRAIN_MCP_PROFILE": "read"}).caller_context,
        }
    ]

    _FakeBackend.answer = {"is_error": True, "content": [{"type": "text", "text": "no access"}]}
    assert apple.icloud_bridge("reminders_list", {}) == "ERROR: no access"


def test_gmail_is_refused(env: ToolEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    env.icloud = lambda name, arguments: calls.append(name) or "answer"

    async def fake_archive(provider: str, uid: str, mailbox: str = "INBOX") -> str:
        calls.append("archive")
        return "Archive"

    monkeypatch.setattr(apple.mail_ops, "archive_message", fake_archive)

    for name, args in (
        ("mail_search", {"provider": "gmail", "query": "school"}),
        ("mail_read", {"provider": "gmail", "uid": "1"}),
        ("mail_archive", {"provider": "gmail", "uid": "1"}),
    ):
        text = _run(env, name, args)
        assert text.startswith("ERROR: "), (name, text)
    assert calls == []
    for tool in TOOLS:
        assert "gmail" not in str(tool.schema), tool.name


def test_calendar_update_refused_when_event_involves_others(
    env: ToolEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    updated: list[dict[str, Any]] = []
    others = {"https://caldav.icloud.com/1/shared.ics": True}

    async def involves(event_url: str) -> bool:
        return others.get(event_url, False)

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        updated.append(payload)
        return {"ok": True, "result": "updated event"}

    monkeypatch.setattr(apple.update_calendar_event, "event_involves_others", involves)
    monkeypatch.setattr(apple.update_calendar_event, "execute", fake_execute)

    shared = _run(
        env,
        "calendar_update",
        {"event_url": "https://caldav.icloud.com/1/shared.ics", "summary": "Moved"},
    )
    own = _run(
        env,
        "calendar_update",
        {"event_url": "https://caldav.icloud.com/1/own.ics", "summary": "Moved"},
    )

    assert shared == (
        "ERROR: this event involves other people; the connector never sends notices, "
        "change it by hand"
    )
    assert own == "updated event"
    assert updated == [{"event_url": "https://caldav.icloud.com/1/own.ics", "summary": "Moved"}]


@pytest.mark.parametrize("zone_name", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_append_without_path_writes_today_in_the_configured_zone(
    env: ToolEnv, zone_name: str
) -> None:
    zone = ZoneInfo(zone_name)
    env = replace(env, zone=zone)
    today = write.daily_note_path(datetime.now(zone))

    text = _run(env, "vault_append", {"text": "- Called the school"})

    assert not text.startswith("ERROR"), text
    assert f"path: {today}" in text
    assert (env.vault / today).is_file()


def _calendar_payloads(
    env: ToolEnv, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_execute(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"ok": True, "result": "done"}

    async def involves(event_url: str) -> bool:
        return False

    monkeypatch.setattr(apple.create_calendar_event, "execute", fake_execute)
    monkeypatch.setattr(apple.update_calendar_event, "execute", fake_execute)
    monkeypatch.setattr(apple.update_calendar_event, "event_involves_others", involves)
    return seen


def test_calendar_times_without_an_offset_are_local_time(
    env: ToolEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _calendar_payloads(env, monkeypatch)
    env = replace(env, zone=ZoneInfo("Europe/Berlin"))

    _run(
        env,
        "calendar_create",
        {"summary": "Dentist", "start": "2026-10-22T15:00", "end": "2026-10-22T16:00"},
    )
    _run(
        env,
        "calendar_update",
        {"event_url": "https://caldav.icloud.com/1/own.ics", "start": "2026-12-01T15:00"},
    )

    assert seen[0]["start"] == "2026-10-22T15:00:00+02:00"
    assert seen[0]["end"] == "2026-10-22T16:00:00+02:00"
    assert seen[1]["start"] == "2026-12-01T15:00:00+01:00"


def test_calendar_times_with_an_offset_or_all_day_pass_unchanged(
    env: ToolEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _calendar_payloads(env, monkeypatch)
    env = replace(env, zone=ZoneInfo("Europe/Berlin"))

    _run(
        env,
        "calendar_create",
        {"summary": "Call", "start": "2026-10-22T15:00:00-04:00", "end": "2026-10-22T16:00Z"},
    )
    _run(
        env,
        "calendar_create",
        {"summary": "Trip", "start": "2026-10-22", "end": "2026-10-24", "all_day": True},
    )

    assert (seen[0]["start"], seen[0]["end"]) == ("2026-10-22T15:00:00-04:00", "2026-10-22T16:00Z")
    assert (seen[1]["start"], seen[1]["end"]) == ("2026-10-22", "2026-10-24")


def test_text_that_is_not_a_time_passes_unchanged() -> None:
    zone = ZoneInfo("Europe/Berlin")
    for value in ("tomorrow at 3", "", "15:00"):
        assert apple.local_time(value, zone) == value


def test_calendar_schemas_say_a_time_without_an_offset_is_local() -> None:
    for name in ("calendar_create", "calendar_update"):
        start = _by_name()[name].schema["properties"]["start"]["description"]
        assert "without an offset" in start, name
