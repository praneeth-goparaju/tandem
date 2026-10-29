"""The connector's integrations, and the one place that decides which tools it serves."""

from __future__ import annotations

from typing import Protocol

from src.brain.connector.config import ConnectorConfig
from src.brain.connector.integrations.apple import (
    AppleCalendar,
    AppleContacts,
    AppleMail,
    AppleReminders,
)
from src.brain.connector.tools import ToolDef, vault_tools


class Integration(Protocol):
    name: str

    def enabled(self, config: ConnectorConfig) -> bool: ...

    def tools(self, config: ConnectorConfig) -> list[ToolDef]: ...


INTEGRATIONS: tuple[Integration, ...] = (
    AppleReminders(),
    AppleCalendar(),
    AppleMail(),
    AppleContacts(),
)


def build_tools(config: ConnectorConfig) -> list[ToolDef]:
    """The vault tools plus the tools of each integration that is on."""
    built = vault_tools()
    for integration in INTEGRATIONS:
        if integration.enabled(config):
            built.extend(integration.tools(config))
    return built
