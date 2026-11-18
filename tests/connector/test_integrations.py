from __future__ import annotations

import asyncio
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.brain.connector.config import ConnectorConfig, Integrations
from src.brain.connector.integrations import apple, build_tools
from src.brain.connector.tools import Call, ToolDef, ToolEnv, run_tool
from src.brain.connector.vault.undo_store import UndoStore
from src.brain.mcp.tools import _icloud

VAULT_FULL = [
    "vault_search",
    "vault_read",
    "vault_list",
    "vault_append",
    "vault_create",
    "vault_edit",
    "vault_edit_confirm",
    "vault_undo",
    "open_item_add",
]
VAULT_LIMITED = {
    "vault_search",
    "vault_read",
    "vault_list",
    "vault_append",
    "vault_create",
    "open_item_add",
}
BY_INTEGRATION = {
    "reminders": ["reminders_list", "reminder_create", "reminder_update", "reminder_complete"],
    "calendar": ["calendar_list", "calendar_create", "calendar_update"],
    "mail": ["mail_search", "mail_read", "mail_archive"],
    "contacts": ["contacts_search"],
}
LIMITED_BY_INTEGRATION = {
    "reminders": {"reminders_list", "reminder_create"},
    "calendar": {"calendar_list"},
    "mail": {"mail_search", "mail_read"},
    "contacts": {"contacts_search"},
}
ALL_ON = Integrations(reminders=True, calendar=True, mail=True, contacts=True)


def _config(integrations: Integrations | None = None) -> ConnectorConfig:
    config = ConnectorConfig(
        vault_path=Path("/no/such/vault"),
        data_dir=Path("/no/such/data"),
        public_url="https://vault.example.com",
        allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
    )
    return config if integrations is None else replace(config, integrations=integrations)


def _full(integrations: Integrations) -> list[str]:
    return [tool.name for tool in build_tools(_config(integrations)) if tool.full]


def _limited(integrations: Integrations) -> set[str]:
    return {tool.name for tool in build_tools(_config(integrations)) if tool.limited}


def test_vault_only_tool_names() -> None:
    assert _full(Integrations()) == VAULT_FULL
    assert _limited(Integrations()) == VAULT_LIMITED


@pytest.mark.parametrize("name", list(BY_INTEGRATION))
def test_each_switch_adds_only_its_own_tools(name: str) -> None:
    one = Integrations(**{name: True})

    assert _full(one) == VAULT_FULL + BY_INTEGRATION[name]
    assert _limited(one) == VAULT_LIMITED | LIMITED_BY_INTEGRATION[name]


def test_all_on_matches_spec_table() -> None:
    full, limited = _full(ALL_ON), _limited(ALL_ON)

    assert len(full) == 20
    assert full == VAULT_FULL + [name for names in BY_INTEGRATION.values() for name in names]
    assert len(limited) == 12
    assert limited == VAULT_LIMITED.union(*LIMITED_BY_INTEGRATION.values())


def test_building_the_tools_never_changes_the_vault_list() -> None:
    build_tools(_config(ALL_ON)).clear()
    build_tools(_config(ALL_ON))

    assert _full(Integrations()) == VAULT_FULL


# Reminders lists and calendars a person may use

SAM = Call(
    person="sam",
    person_name="Sam",
    reminder_lists=("Family", "Shopping"),
    calendars=("Family",),
)
ALEX = Call(person="alex", person_name="Alex")
REMINDER_LINES = {
    "Family": "• [Family] | title=[Milk] | due=— | alarms=0 | p=0 | id=[r1]",
    "Shopping": "• [Shopping] | title=[Bread] | due=— | alarms=0 | p=0 | id=[r2]",
    "Family Private": "• [Family Private] | title=[Gift] | due=— | alarms=0 | p=0 | id=[r3]",
}
EVENT_LINES = {
    "Family": "2026-12-02 09:00 → 2026-12-02 10:00 | Family | [School] | https://x/f/1.ics",
    "Family Trips": "2026-12-01 08:00 → 2026-12-01 09:00 | Family Trips | [Train] | https://x/t/1.ics",
    "Work": "2026-12-01 07:00 → 2026-12-01 08:00 | Work | [Standup] | https://x/w/1.ics",
}


class Bridge:
    """The iCloud bridge: records each call and answers as the brain tools do, by substring."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, dict(arguments)))
        lines = REMINDER_LINES if name == "reminders_list" else EVENT_LINES
        key = "list_name" if name == "reminders_list" else "calendar_name"
        wanted = str(arguments.get(key) or "").strip().lower()
        return "\n".join(line for title, line in lines.items() if wanted in title.lower())


@pytest.fixture
def bridge() -> Bridge:
    return Bridge()


@pytest.fixture
def tool_env(tmp_path: Path, bridge: Bridge) -> ToolEnv:
    return ToolEnv(vault=tmp_path, undo=UndoStore(tmp_path / "undo.db"), icloud=bridge)


def _tools() -> dict[str, ToolDef]:
    return {tool.name: tool for tool in build_tools(_config(ALL_ON))}


def _call_tool(env: ToolEnv, name: str, args: dict[str, Any], call: Call) -> str:
    return asyncio.run(run_tool(_tools()[name], env, args, call))


class Handlers:
    """Stand-ins for the reminder and calendar handlers, recording their payloads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.payloads: dict[str, list[dict[str, Any]]] = {}
        self.lists = {"r-family": "Family", "r-private": "Private"}
        self.calendars = {"https://x/f/1.ics": "Family", "https://x/w/1.ics": "Work"}
        for module, name in (
            (apple.create_reminder, "reminder_create"),
            (apple.update_reminder, "reminder_update"),
            (apple.complete_reminder, "reminder_complete"),
            (apple.create_calendar_event, "calendar_create"),
            (apple.update_calendar_event, "calendar_update"),
        ):
            monkeypatch.setattr(module, "execute", self._execute(name))
        monkeypatch.setattr(apple.update_reminder, "list_of", self.lists.get)
        monkeypatch.setattr(apple.update_calendar_event, "calendar_of", self.calendars.get)

        async def no_one_else(event_url: str) -> bool:
            return False

        monkeypatch.setattr(apple.update_calendar_event, "event_involves_others", no_one_else)

    def _execute(self, name: str) -> Any:
        async def execute(payload: dict[str, Any]) -> dict[str, Any]:
            self.payloads.setdefault(name, []).append(payload)
            return {"ok": True, "result": f"{name} done"}

        return execute


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> Handlers:
    return Handlers(monkeypatch)


def test_a_reminder_in_a_list_the_person_may_not_use_is_refused(
    tool_env: ToolEnv, handlers: Handlers
) -> None:
    refused = _call_tool(tool_env, "reminder_complete", {"id": "r-private"}, SAM)
    updated = _call_tool(tool_env, "reminder_update", {"id": "r-private", "title": "x"}, SAM)
    done = _call_tool(tool_env, "reminder_complete", {"id": "r-family"}, SAM)
    unknown = _call_tool(tool_env, "reminder_complete", {"id": "r-gone"}, SAM)

    assert refused == "ERROR: Sam can use these lists: Family, Shopping"
    assert updated == refused
    assert done == "reminder_complete done"
    assert unknown.startswith("ERROR:") and "r-gone" in unknown
    assert handlers.payloads["reminder_complete"] == [{"id": "r-family"}]
    assert "reminder_update" not in handlers.payloads


def test_an_allowed_list_name_in_another_case_or_with_spaces_works(
    tool_env: ToolEnv, handlers: Handlers
) -> None:
    args = {"title": "Milk", "due": "2026-12-01T09:00"}
    created = _call_tool(tool_env, "reminder_create", {**args, "list_name": " family "}, SAM)
    refused = _call_tool(tool_env, "reminder_create", {**args, "list_name": "Private"}, SAM)
    near = _call_tool(tool_env, "reminder_create", {**args, "list_name": "Fam"}, SAM)

    assert created == "reminder_create done"
    assert handlers.payloads["reminder_create"] == [{**args, "list_name": "Family"}]
    assert refused == "ERROR: Sam can use these lists: Family, Shopping"
    assert near == refused


def test_reminders_list_runs_once_per_allowed_list(tool_env: ToolEnv, bridge: Bridge) -> None:
    every = _call_tool(tool_env, "reminders_list", {}, SAM)
    one = _call_tool(tool_env, "reminders_list", {"list_name": "FAMILY"}, SAM)
    refused = _call_tool(tool_env, "reminders_list", {"list_name": "Private"}, SAM)

    assert [args.get("list_name") for _, args in bridge.calls] == ["Family", "Shopping", "Family"]
    assert every.splitlines() == [REMINDER_LINES["Family"], REMINDER_LINES["Shopping"]]
    assert one.splitlines() == [REMINDER_LINES["Family"]]
    assert "Gift" not in every + one
    assert refused == "ERROR: Sam can use these lists: Family, Shopping"


def test_calendar_create_uses_the_first_allowed_calendar(
    tool_env: ToolEnv, handlers: Handlers
) -> None:
    args = {"summary": "School", "start": "2026-12-01T09:00", "end": "2026-12-01T10:00"}
    _call_tool(tool_env, "calendar_create", args, SAM)
    _call_tool(tool_env, "calendar_create", {**args, "calendar_name": "family "}, SAM)
    refused = _call_tool(tool_env, "calendar_create", {**args, "calendar_name": "Work"}, SAM)

    assert [p["calendar_name"] for p in handlers.payloads["calendar_create"]] == ["Family", "Family"]
    assert refused == "ERROR: Sam can use these calendars: Family"


def test_calendar_update_checks_the_events_calendar(tool_env: ToolEnv, handlers: Handlers) -> None:
    done = _call_tool(
        tool_env, "calendar_update", {"event_url": "https://x/f/1.ics", "summary": "A"}, SAM
    )
    refused = _call_tool(
        tool_env, "calendar_update", {"event_url": "https://x/w/1.ics", "summary": "B"}, SAM
    )

    assert done == "calendar_update done"
    assert refused == "ERROR: Sam can use these calendars: Family"
    assert len(handlers.payloads["calendar_update"]) == 1


def test_calendar_list_keeps_to_the_allowed_calendars(tool_env: ToolEnv, bridge: Bridge) -> None:
    every = _call_tool(tool_env, "calendar_list", {"date_from": "2026-12-01"}, SAM)
    named = _call_tool(tool_env, "calendar_list", {"calendar_name": "Family"}, SAM)
    refused = _call_tool(tool_env, "calendar_list", {"calendar_name": "Work"}, SAM)

    assert [args.get("calendar_name") for _, args in bridge.calls] == ["Family", "Family"]
    assert bridge.calls[0][1]["date_from"] == "2026-12-01"
    assert every.splitlines() == [EVENT_LINES["Family"]]
    assert named == every
    assert refused == "ERROR: Sam can use these calendars: Family"


def test_a_person_without_allowed_names_is_refused_nothing(
    tool_env: ToolEnv, bridge: Bridge, handlers: Handlers
) -> None:
    assert _call_tool(tool_env, "reminder_complete", {"id": "r-private"}, ALEX) == (
        "reminder_complete done"
    )
    every = _call_tool(tool_env, "reminders_list", {}, ALEX)
    assert bridge.calls == [("reminders_list", {})]
    assert "Gift" in every
    args = {"summary": "S", "start": "2026-12-01T09:00", "end": "2026-12-01T10:00"}
    _call_tool(tool_env, "calendar_create", {**args, "calendar_name": "Work"}, ALEX)
    assert handlers.payloads["calendar_create"][0]["calendar_name"] == "Work"
    _call_tool(tool_env, "calendar_create", args, ALEX)
    assert "calendar_name" not in handlers.payloads["calendar_create"][1]
    assert _call_tool(tool_env, "calendar_list", {}, ALEX) == "\n".join(EVENT_LINES.values())


class _Calendar:
    def __init__(self, name: str) -> None:
        self.name = name
        self.saved: list[str] = []

    def get_display_name(self) -> str:
        return self.name

    def save_event(self, ics: str) -> Any:
        self.saved.append(ics)
        return types.SimpleNamespace(url=f"https://x/{self.name}/1.ics")


class _Client:
    def __init__(self, calendars: list[_Calendar]) -> None:
        self.calendars = calendars

    def principal(self) -> Any:
        return types.SimpleNamespace(calendars=lambda: self.calendars)


def test_an_exact_calendar_name_wins_and_a_vague_one_is_refused(
    tool_env: ToolEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    family, trips = _Calendar("Family"), _Calendar("Family Trips")
    monkeypatch.setattr(_icloud, "caldav_client", lambda *a, **k: _Client([trips, family]))
    args = {"summary": "School", "start": "2026-12-01T09:00", "end": "2026-12-01T10:00"}

    exact = _call_tool(tool_env, "calendar_create", {**args, "calendar_name": " family"}, ALEX)
    vague = _call_tool(tool_env, "calendar_create", {**args, "calendar_name": "Fam"}, ALEX)
    one = _call_tool(tool_env, "calendar_create", {**args, "calendar_name": "trips"}, ALEX)

    assert exact.startswith("created event") and len(family.saved) == 1
    assert vague == "ERROR: more than one calendar matches Fam: Family Trips, Family"
    assert one.startswith("created event") and len(trips.saved) == 1


# Each person's own iCloud account

SAM_ACCOUNT = replace(
    SAM, icloud_apple_id="sam@example.com", icloud_imap_user="sam@mail.example.com"
)


class AccountBridge:
    """A bridge that records the extra environment each call gets."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    def __call__(self, name: str, arguments: dict[str, Any], *extra: Any) -> str:
        self.calls.append((name, arguments, dict(extra[0]) if extra else None))
        return f"{name} answered"


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    keychain = {"sam@example.com": "sam-app-password"}
    monkeypatch.setattr(_icloud, "_keychain_password", lambda apple_id: keychain.get(apple_id, ""))
    return keychain


def test_sams_mail_search_runs_on_her_account(signed_in: dict[str, str], tmp_path: Path) -> None:
    bridge = AccountBridge()
    env = ToolEnv(vault=tmp_path, undo=UndoStore(tmp_path / "undo.db"), icloud=bridge)

    _call_tool(env, "mail_search", {"query": "school"}, SAM_ACCOUNT)
    _call_tool(env, "contacts_search", {"query": "school"}, SAM_ACCOUNT)
    _call_tool(env, "mail_search", {"query": "school"}, ALEX)

    sam_mail, sam_contacts, alex_mail = bridge.calls
    assert sam_mail[2] == {
        "ICLOUD_APPLE_ID": "sam@example.com",
        "ICLOUD_IMAP_USER": "sam@mail.example.com",
        "ICLOUD_APP_PASSWORD": "",
    }
    assert "sam-app-password" not in str(sam_mail)
    assert sam_contacts[2] == sam_mail[2]
    assert alex_mail[2] is None


def test_an_account_without_a_keychain_item_is_refused_by_name(tmp_path: Path) -> None:
    bridge = AccountBridge()
    env = ToolEnv(vault=tmp_path, undo=UndoStore(tmp_path / "undo.db"), icloud=bridge)
    sam = replace(SAM_ACCOUNT, calendars=None)

    texts = [
        _call_tool(env, "mail_search", {"query": "x"}, sam),
        _call_tool(env, "mail_read", {"uid": "1"}, sam),
        _call_tool(env, "contacts_search", {"query": "x"}, sam),
        _call_tool(env, "calendar_list", {}, sam),
        _call_tool(env, "mail_archive", {"uid": "1"}, sam),
        _call_tool(env, "calendar_create", {"summary": "S", "start": "2026-12-01T09:00", "end": "2026-12-01T10:00"}, sam),
        _call_tool(env, "calendar_update", {"event_url": "https://x/f/1.ics", "summary": "A"}, sam),
    ]

    assert texts == ["ERROR: Sam's iCloud account is not signed in on this Mac yet"] * len(texts)
    assert bridge.calls == []


def test_sams_calendar_and_archive_handlers_get_her_account(
    signed_in: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def create(payload: dict[str, Any], **account: Any) -> dict[str, Any]:
        seen.append(("create", account))
        return {"ok": True, "result": "created"}

    async def update(payload: dict[str, Any], **account: Any) -> dict[str, Any]:
        seen.append(("update", account))
        return {"ok": True, "result": "updated"}

    async def involves(event_url: str, **account: Any) -> bool:
        seen.append(("involves", account))
        return False

    def calendar_of(event_url: str, **account: Any) -> str:
        seen.append(("calendar_of", account))
        return "Family"

    async def archive(provider: str, uid: str, mailbox: str = "INBOX", **account: Any) -> str:
        seen.append(("archive", account))
        return "Archive"

    monkeypatch.setattr(apple.create_calendar_event, "execute", create)
    monkeypatch.setattr(apple.update_calendar_event, "execute", update)
    monkeypatch.setattr(apple.update_calendar_event, "event_involves_others", involves)
    monkeypatch.setattr(apple.update_calendar_event, "calendar_of", calendar_of)
    monkeypatch.setattr(apple.mail_ops, "archive_message", archive)
    env = ToolEnv(vault=tmp_path, undo=UndoStore(tmp_path / "undo.db"), icloud=AccountBridge())
    event = {"summary": "S", "start": "2026-12-01T09:00", "end": "2026-12-01T10:00"}

    assert _call_tool(env, "calendar_create", event, SAM_ACCOUNT) == "created"
    assert _call_tool(env, "calendar_update", {"event_url": "https://x/1.ics", "summary": "A"}, SAM_ACCOUNT) == "updated"
    assert not _call_tool(env, "mail_archive", {"uid": "7"}, SAM_ACCOUNT).startswith("ERROR")
    sam_seen = list(seen)
    seen.clear()
    _call_tool(env, "calendar_create", event, ALEX)
    _call_tool(env, "mail_archive", {"uid": "7"}, ALEX)

    account = {"apple_id": "sam@example.com"}
    assert sam_seen == [
        ("create", account),
        ("calendar_of", account),
        ("involves", account),
        ("update", account),
        ("archive", {**account, "imap_user": "sam@mail.example.com"}),
    ]
    assert seen == [("create", {}), ("archive", {})]


def test_the_bridge_hands_the_account_to_the_tool_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    class Backend:
        def __init__(self, settings: Any) -> None:
            pass

        def invoke(self, operation: str, payload: dict[str, Any], **kwargs: Any) -> Any:
            seen.append(kwargs.get("environment"))
            return {"is_error": False, "content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(apple, "CommandBackend", Backend)
    monkeypatch.setattr(apple.BridgeSettings, "from_environment", lambda environ: None)

    assert apple.icloud_bridge("mail_search", {}) == "ok"
    assert apple.icloud_bridge("mail_search", {}, {"ICLOUD_APPLE_ID": "sam@example.com"}) == "ok"
    assert seen == [None, {"ICLOUD_APPLE_ID": "sam@example.com"}]


def test_a_second_person_without_an_account_never_gets_the_macs_account(tmp_path: Path) -> None:
    bridge = AccountBridge()
    env = ToolEnv(vault=tmp_path, undo=UndoStore(tmp_path / "undo.db"), icloud=bridge)
    sam = replace(
        SAM_ACCOUNT, icloud_apple_id=None, icloud_imap_user=None, calendars=None, default_icloud=False
    )

    texts = [
        _call_tool(env, "mail_search", {"query": "x"}, sam),
        _call_tool(env, "mail_read", {"uid": "1"}, sam),
        _call_tool(env, "contacts_search", {"query": "x"}, sam),
        _call_tool(env, "calendar_list", {}, sam),
        _call_tool(env, "mail_archive", {"uid": "1"}, sam),
        _call_tool(
            env,
            "calendar_create",
            {"summary": "S", "start": "2026-12-01T09:00", "end": "2026-12-01T10:00"},
            sam,
        ),
        _call_tool(env, "calendar_update", {"event_url": "https://x/f/1.ics", "summary": "A"}, sam),
    ]

    assert texts == ["ERROR: Sam has no iCloud account set up for this connector yet"] * len(texts)
    assert bridge.calls == []
