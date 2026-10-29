from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from src.brain.actions.fingerprints import dedupe_key, is_sha256_hex, payload_hash
from src.db import utcnow

MAX_VAULT_FILE_CHANGES = 8
_MAX_VAULT_NOTE_PATH_CHARS = 240


def validate_vault_note_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("vault edit path must be a string")
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > _MAX_VAULT_NOTE_PATH_CHARS
        or unicodedata.normalize("NFC", value) != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(unicodedata.category(char) == "Cf" or char == "\\" for char in value)
        or path.is_absolute()
        or path.as_posix() != value
        or path.suffix.casefold() != ".md"
        or any(part in {".", ".."} or part.startswith(".") for part in path.parts)
    ):
        raise ValueError("vault edit path must be a normalized, visible .md path")
    return value


def validate_expected_file_sha256(value: object) -> str | None:
    """Validate an immutable file-version token; ``None`` means absent."""
    if value is None:
        return None
    if not is_sha256_hex(value):
        raise ValueError("expected_sha256 must be a SHA-256 digest or null")
    assert isinstance(value, str)
    return value.casefold()


class ActionType(str, Enum):
    SEND_DRAFT = "send_draft"
    SEND_BOT_MESSAGE = "send_bot_message"
    PUSH_NOTIFICATION = "push_notification"
    CREATE_TODOIST_TASK = "create_todoist_task"
    EDIT_VAULT_FILE = "edit_vault_file"
    EDIT_VAULT_FILES = "edit_vault_files"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    UPDATE_CALENDAR_EVENT = "update_calendar_event"
    MAIL_ARCHIVE = "mail_archive"
    MAIL_ARCHIVE_BATCH = "mail_archive_batch"
    CREATE_REMINDER = "create_reminder"
    COMPLETE_REMINDER = "complete_reminder"
    CREATE_CONTACT = "create_contact"
    UPDATE_CONTACT = "update_contact"
    PROMOTE_INTAKE = "promote_intake"
    PROMOTE_NOTE = "promote_note"
    INSERT_VAULT_LINE = "insert_vault_line"


class Risk(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    MANUAL = "manual"


AUTO_ELIGIBLE_TYPES: frozenset[ActionType] = frozenset({
    ActionType.PUSH_NOTIFICATION,
    ActionType.CREATE_TODOIST_TASK,
    ActionType.CREATE_REMINDER,
    ActionType.COMPLETE_REMINDER,
    ActionType.CREATE_CALENDAR_EVENT,
    ActionType.UPDATE_CALENDAR_EVENT,
})

OWN_DEVICE_AUTO_TYPES: frozenset[ActionType] = frozenset({
    ActionType.CREATE_REMINDER,
    ActionType.COMPLETE_REMINDER,
    ActionType.CREATE_TODOIST_TASK,
    ActionType.CREATE_CALENDAR_EVENT,
    ActionType.UPDATE_CALENDAR_EVENT,
})


TRUSTED_CLIENT_ACTION_TYPES: frozenset[ActionType] = frozenset({
    ActionType.COMPLETE_REMINDER,
    ActionType.MAIL_ARCHIVE,
    ActionType.MAIL_ARCHIVE_BATCH,
    ActionType.SEND_BOT_MESSAGE,
})
TRUSTED_CLIENT_APPROVAL_CHANNEL = "trusted_client"

# Actions whose handler uses Apple Reminders through EventKit. macOS allows
# that only in a process whose responsible process holds the Reminders grant.
# propose_action leaves these to the background service's executor when its
# own process has no access (a bridge started from the Claude desktop app).
REMINDERS_ACCESS_TYPES: frozenset[ActionType] = frozenset({
    ActionType.CREATE_REMINDER,
    ActionType.COMPLETE_REMINDER,
})


HUMAN_APPROVAL_CHANNELS: frozenset[str] = frozenset({
    "slack_reply",
    "slack_button",
    "signal_reply",
})


class Status(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    PARTIAL = "partial"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


@dataclass
class Action:
    id: str
    type: ActionType
    payload: dict[str, Any]
    rationale: str
    source_finding_ids: list[str] = field(default_factory=list)
    risk: Risk = Risk.CONFIRM
    status: Status = Status.PENDING
    origin_agent: str | None = None
    origin_area: str | None = None
    approval_audience: str | None = None
    occurrence_key: str | None = None
    expires_at: str | None = None
    created_at: str = field(default_factory=utcnow)

    def to_row(self) -> dict[str, Any]:
        payload_json = json.dumps(self.payload, sort_keys=True)
        return {
            "id": self.id,
            "type": self.type.value,
            "payload_json": payload_json,
            "payload_hash": payload_hash(payload_json),
            "dedupe_key": dedupe_key(
                action_type=self.type.value,
                payload=self.payload,
                origin_area=self.origin_area,
                version=1,
                occurrence_key=self.occurrence_key,
            ),
            "version": 1,
            "occurrence_key": self.occurrence_key,
            "rationale": self.rationale,
            "source_finding_ids": ",".join(self.source_finding_ids),
            "risk": self.risk.value,
            "status": self.status.value,
            "approval_channel": None,
            "approval_signal": None,
            "approval_actor": None,
            "origin_agent": self.origin_agent,
            "origin_area": self.origin_area,
            "approval_audience": self.approval_audience or (
                f"area:{self.origin_area}" if self.origin_area else "owner"
            ),
            "created_at": self.created_at,
            "decided_at": None,
            "executed_at": None,
            "result": None,
            "expires_at": self.expires_at,
        }
