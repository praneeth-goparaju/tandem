from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.brain.connector.config import ConnectorConfig, Integrations
from src.brain.connector.integrations import build_tools

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
