from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.brain.connector.config import ConnectorConfig, Integrations
from src.brain.connector.layout import DEFAULT_LAYOUT
from src.brain.connector.people import Person


@pytest.fixture(autouse=True)
def private_overlay_is_a_temp_folder(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point ``USER_DIR`` at an empty temp folder so no connector test reads ``user/``."""
    overlay = tmp_path_factory.mktemp("overlay")
    monkeypatch.setenv("USER_DIR", str(overlay))
    return overlay


SAM_EXCLUDED = frozenset(
    {"mail_search", "mail_read", "mail_archive", "contacts_search"}
)


def alex_and_sam(
    overlay: Path,
    *,
    excluded: frozenset[str] = SAM_EXCLUDED,
    reminder_lists: tuple[str, ...] | None = ("Family", "Shopping"),
    calendars: tuple[str, ...] | None = ("Family",),
    sam_apple_id: str | None = "sam@example.com",
) -> tuple[Person, Person]:
    """Two people as in the example settings: alex at /mcp, sam at /sam/mcp."""
    alex = Person(
        key="alex",
        name="Alex",
        prefix="",
        excluded_tools=frozenset(),
        icloud_apple_id=None,
        icloud_imap_user=None,
        reminder_lists=None,
        calendars=None,
        layout=DEFAULT_LAYOUT,
        instructions_overlay=overlay / "connector_instructions.yaml",
    )
    sam = Person(
        key="sam",
        name="Sam",
        prefix="sam",
        excluded_tools=excluded,
        icloud_apple_id=sam_apple_id,
        icloud_imap_user=None,
        reminder_lists=reminder_lists,
        calendars=calendars,
        layout=replace(
            DEFAULT_LAYOUT,
            daily_notes_dir="01 - Daily Notes/Sam",
            daily_note_template="Daily Note - Sam.md",
        ),
        instructions_overlay=overlay / "connector_instructions.sam.yaml",
    )
    return alex, sam


@pytest.fixture
def make_people(private_overlay_is_a_temp_folder: Path) -> Callable[..., tuple[Person, Person]]:
    """Build alex and sam, with sam's exclusions, lists and calendars as the test asks."""

    def build(**changes: Any) -> tuple[Person, Person]:
        return alex_and_sam(private_overlay_is_a_temp_folder, **changes)

    return build


CONNECTOR_VAULT = Path(__file__).parent.parent / "fixtures" / "connector_vault"
SAM_TEMPLATE = "---\ntype: daily-note\nperson: sam\n---\n# {{date:YYYY-MM-DD}}\n\n## Notes\n"


def people_config(tmp_path: Path, people: tuple[Person, ...]) -> ConnectorConfig:
    """A connector on a copy of the test vault, with every integration on, for ``people``.

    The vault gets sam's daily note template next to the usual one.
    """
    vault = tmp_path / "Vault"
    if not vault.exists():
        shutil.copytree(CONNECTOR_VAULT, vault)
        (vault / "11 - Templates" / "Daily Note - Sam.md").write_text(SAM_TEMPLATE, encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700, exist_ok=True)
    return ConnectorConfig(
        vault_path=vault,
        data_dir=data_dir,
        public_url="https://vault.example.com",
        allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
        integrations=Integrations(reminders=True, calendar=True, mail=True, contacts=True),
        people=people,
    )


@pytest.fixture
def two_people_config(
    tmp_path: Path, make_people: Callable[..., tuple[Person, Person]]
) -> ConnectorConfig:
    """alex at /mcp with every tool; sam at /sam/mcp without the mail and contacts tools."""
    return people_config(tmp_path, make_people())
