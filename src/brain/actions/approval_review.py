from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from src.brain.actions.bot_message_policy import normalize_bot_message_payload
from src.brain.actions.fingerprints import is_sha256_hex, payload_hash
from src.brain.actions.handlers.create_contact import is_vcard_type_token
from src.brain.actions.types import (
    MAX_VAULT_FILE_CHANGES,
    ActionType,
    validate_expected_file_sha256,
    validate_vault_note_path,
)

# Slack allows 50 blocks per message. The notifier splits complete reviews into
# 2,800-character plain-text blocks and reserves one block for the reply plus
# one for controls. Effects that exceed this practical ceiling fail closed.
MAX_SUMMARY_CHARS = 120_000
MAX_PURPOSE_CHARS = 240
MAX_ARCHIVE_BATCH = 50
_BATCH_SENDERS_SHOWN = 12

_ACTION_LABELS: dict[ActionType, str] = {
    ActionType.SEND_DRAFT: "Send message",
    ActionType.SEND_BOT_MESSAGE: "Send Brain bot message",
    ActionType.PUSH_NOTIFICATION: "Push notification",
    ActionType.CREATE_TODOIST_TASK: "Create reminder",
    ActionType.EDIT_VAULT_FILE: "Update note",
    ActionType.EDIT_VAULT_FILES: "Update notes",
    ActionType.CREATE_CALENDAR_EVENT: "Add calendar event",
    ActionType.UPDATE_CALENDAR_EVENT: "Update calendar event",
    ActionType.MAIL_ARCHIVE: "Archive email",
    ActionType.MAIL_ARCHIVE_BATCH: "Archive emails",
    ActionType.CREATE_REMINDER: "Create reminder",
    ActionType.COMPLETE_REMINDER: "Complete reminder",
    ActionType.CREATE_CONTACT: "Add contact",
    ActionType.UPDATE_CONTACT: "Update contact",
    ActionType.PROMOTE_INTAKE: "Save captured knowledge",
    ActionType.PROMOTE_NOTE: "Save knowledge note",
    ActionType.INSERT_VAULT_LINE: "Add line to note",
}

_SUMMARY_FIELDS: dict[ActionType, tuple[tuple[str, str], ...]] = {
    ActionType.SEND_DRAFT: (
        ("draft_title", "Title"),
        ("draft_preview", "Preview"),
        ("draft_path", "Path"),
        ("draft_kind", "Kind"),
        ("draft_recipient", "Recipient"),
        ("draft_size", "Bytes"),
        ("draft_sha256", "SHA-256"),
    ),
    ActionType.SEND_BOT_MESSAGE: (
        ("destination_area", "Brain area"),
        ("title", "Title"),
        ("message", "Message"),
    ),
    ActionType.PUSH_NOTIFICATION: (
        ("title", "Title"),
        ("message", "Message"),
        ("thread_detail", "Thread detail"),
        ("area", "Area"),
        ("agent", "Agent"),
        ("finding_id", "Finding"),
        ("proposal_action_ids", "Attached approvals"),
        ("media_ids", "Attached media"),
        ("occasion_person_id", "Occasion person"),
        ("occasion", "Occasion"),
        ("occasion_year", "Occasion year"),
    ),
    ActionType.CREATE_TODOIST_TASK: (
        ("content", "Title"),
        ("description", "Description"),
        ("due_date", "Due"),
        ("due_string", "Due"),
        ("project_id", "List"),
        ("priority", "Priority"),
        ("labels", "Labels"),
        ("action_note_id", "Action note"),
    ),
    ActionType.EDIT_VAULT_FILE: (("path", "Path"),),
    ActionType.EDIT_VAULT_FILES: (("summary", "Summary"),),
    ActionType.CREATE_CALENDAR_EVENT: (
        ("summary", "Title"),
        ("start", "Start"),
        ("end", "End"),
        ("all_day", "All day"),
        ("location", "Location"),
        ("description", "Description"),
        ("calendar_name", "Calendar"),
        ("attendees", "Attendees"),
        ("alarm_minutes_before", "Alarm minutes before"),
    ),
    ActionType.UPDATE_CALENDAR_EVENT: (
        ("summary", "Title"),
        ("event_url", "Event"),
        ("start", "Start"),
        ("end", "End"),
        ("location", "Location"),
        ("description", "Description"),
        ("cancelled", "Cancelled"),
    ),
    ActionType.MAIL_ARCHIVE: (
        ("message_from", "From"),
        ("message_subject", "Subject"),
        ("message_id", "Message-ID"),
        ("provider", "Provider"),
        ("mailbox", "Mailbox"),
        ("uid", "Message"),
        ("uidvalidity", "UIDVALIDITY"),
        ("message_header_sha256", "Header SHA-256"),
    ),
    ActionType.MAIL_ARCHIVE_BATCH: (
        ("summary", "Summary"),
        ("messages", "Messages"),
    ),
    ActionType.CREATE_REMINDER: (
        ("title", "Title"),
        ("due", "Due"),
        ("list_name", "List"),
        ("notes", "Notes"),
        ("priority", "Priority"),
        ("alarm_minutes_before", "Alarm minutes before"),
    ),
    ActionType.COMPLETE_REMINDER: (
        ("id", "Reminder ID"),
    ),
    ActionType.CREATE_CONTACT: (
        ("full_name", "Name"),
        ("given_name", "Given name"),
        ("family_name", "Family name"),
        ("emails", "Emails"),
        ("tels", "Phones"),
        ("org", "Organization"),
        ("title", "Title"),
        ("note", "Note"),
    ),
    ActionType.UPDATE_CONTACT: (
        ("href", "Contact"),
        ("full_name", "Name"),
        ("given_name", "Given name"),
        ("family_name", "Family name"),
        ("set_emails", "Set emails"),
        ("add_emails", "Add emails"),
        ("set_tels", "Set phones"),
        ("add_tels", "Add phones"),
        ("org", "Organization"),
        ("title", "Title"),
        ("note", "Note"),
        ("note_append", "Append note"),
    ),
    ActionType.PROMOTE_INTAKE: (
        ("intake_id", "Capture"),
        ("topic", "Topic"),
        ("intake_source", "Source"),
        ("intake_source_id", "Source ID"),
        ("intake_preview", "Preview"),
        ("intake_size", "Bytes"),
        ("intake_sha256", "SHA-256"),
    ),
    ActionType.PROMOTE_NOTE: (
        ("title", "Title"),
        ("body", "Body"),
        ("area", "Area"),
        ("agent", "Agent"),
        ("proposing_agent", "Proposing agent"),
    ),
    ActionType.INSERT_VAULT_LINE: (
        ("path", "Target"),
        ("anchor", "After line"),
        ("text", "Add"),
    ),
}

_ALLOWED_FIELDS: dict[ActionType, frozenset[str]] = {
    action_type: frozenset(key for key, _label in fields)
    for action_type, fields in _SUMMARY_FIELDS.items()
}
_ALLOWED_FIELDS[ActionType.EDIT_VAULT_FILE] = frozenset({
    "path", "new_content", "preserve_sections", "expected_sha256",
})
_ALLOWED_FIELDS[ActionType.EDIT_VAULT_FILES] = frozenset({"summary", "changes"})
_ALLOWED_FIELDS[ActionType.SEND_DRAFT] |= frozenset({"area", "agent"})

_REQUIRED_NONEMPTY: dict[ActionType, tuple[str, ...]] = {
    ActionType.SEND_DRAFT: ("draft_path", "draft_sha256"),
    ActionType.SEND_BOT_MESSAGE: ("destination_area", "message"),
    ActionType.PUSH_NOTIFICATION: ("message",),
    ActionType.CREATE_TODOIST_TASK: ("content", "project_id"),
    ActionType.EDIT_VAULT_FILE: ("path",),
    ActionType.CREATE_CALENDAR_EVENT: ("summary", "start", "end"),
    ActionType.UPDATE_CALENDAR_EVENT: ("event_url",),
    ActionType.MAIL_ARCHIVE: ("provider", "uid"),
    ActionType.MAIL_ARCHIVE_BATCH: ("summary",),
    ActionType.CREATE_REMINDER: ("title", "due"),
    ActionType.COMPLETE_REMINDER: ("id",),
    ActionType.CREATE_CONTACT: ("full_name",),
    ActionType.UPDATE_CONTACT: ("href",),
    ActionType.PROMOTE_INTAKE: ("intake_id", "intake_source", "intake_source_id"),
    ActionType.PROMOTE_NOTE: ("body",),
    ActionType.INSERT_VAULT_LINE: ("path", "anchor", "text"),
}

_NULLABLE_FIELDS: dict[ActionType, frozenset[str]] = {
    ActionType.PUSH_NOTIFICATION: frozenset({"finding_id"}),
    ActionType.CREATE_TODOIST_TASK: frozenset({
        "priority", "due_date", "due_string", "labels", "action_note_id",
    }),
}

_LIST_FIELDS = frozenset({
    "add_emails",
    "add_tels",
    "attendees",
    "emails",
    "labels",
    "media_ids",
    "messages",
    "proposal_action_ids",
    "set_emails",
    "set_tels",
    "tels",
})
_BOOLEAN_FIELDS = frozenset({"all_day", "cancelled"})
_INTEGER_FIELDS = frozenset({
    "alarm_minutes_before", "draft_size", "intake_size", "occasion_year", "priority",
})


_HIDDEN_SUMMARY_FIELDS: dict[ActionType, frozenset[str]] = {
    ActionType.MAIL_ARCHIVE: frozenset({
        "message_id", "provider", "mailbox", "uid", "uidvalidity",
        "message_header_sha256",
    }),
}

_READABLE_MARKERS = str.maketrans({"<": "‹", ">": "›", "&": "＆"})


def summary_fields(action_type: ActionType) -> tuple[tuple[str, str], ...]:
    hidden = _HIDDEN_SUMMARY_FIELDS.get(action_type, frozenset())
    return tuple(
        (key, label)
        for key, label in _SUMMARY_FIELDS.get(action_type, ())
        if key not in hidden
    )


def _summary_value(value: Any) -> str:
    if isinstance(value, str):
        return _display_value(value.translate(_READABLE_MARKERS))
    return _display_value(value)


@dataclass(frozen=True)
class ApprovalReview:
    approvable: bool
    heading: str = "Approval unavailable"
    purpose: str = ""
    summary_lines: tuple[str, ...] = ()
    payload_sha256: str = ""
    reason: str = ""
    title: str = ""
    note: str = ""


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey
        value[key] = item
    return value


def _reject_nonfinite(_value: str) -> None:
    raise ValueError


def _slack_safe_json(value: Any, *, indent: int | None = None) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        indent=indent,
        sort_keys=True,
    )
    return (
        encoded.replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("&", r"\u0026")
    )


def _slack_safe_text(value: str) -> str:
    return "".join(
        char
        if (
            char in "\n\t\u200c\u200d"
            or (
                not unicodedata.category(char).startswith("C")
                and unicodedata.category(char) not in {"Zl", "Zp"}
                and char not in "<>&"
            )
        )
        else _slack_safe_json(char)[1:-1]
        for char in value
    )


def _display_value(value: Any, limit: int | None = None) -> str:
    if isinstance(value, str):
        encoded = _slack_safe_text(value).replace("\n", "\n  ↳ ")
        if not encoded:
            return "(empty)"
    else:
        encoded = _slack_safe_json(value)
    if limit is None or len(encoded) <= limit:
        return encoded
    return encoded[: max(1, limit - 1)] + "…"


def _text_preview(value: Any, *, limit: int = 180) -> str:
    if not isinstance(value, str):
        return _display_value(value, limit)
    compact = " ".join(value.split())
    return _display_value(compact, limit) if compact else "(empty)"


def _vault_change_lines(change: Mapping[str, Any]) -> tuple[str, ...]:
    from src.brain.actions.handlers.edit_vault_file import merge_final_content
    from src.brain.atomic_io import confined_path, read_regular_bytes
    from src.orchestrator.config import VAULT_PATH

    rel = validate_vault_note_path(change.get("path"))
    expected = validate_expected_file_sha256(change.get("expected_sha256"))
    target = confined_path(VAULT_PATH, rel)
    try:
        current_bytes = read_regular_bytes(target)
    except FileNotFoundError:
        current_bytes = None
    except OSError as exc:
        raise ValueError("vault target is not safely readable") from exc
    actual = (
        hashlib.sha256(current_bytes).hexdigest()
        if current_bytes is not None else None
    )
    if actual != expected:
        raise ValueError("vault target changed after proposal; review a new action")
    try:
        current = current_bytes.decode() if current_bytes is not None else None
    except UnicodeDecodeError as exc:
        raise ValueError("vault target is not UTF-8 text") from exc
    final = merge_final_content(
        current, change["new_content"], change.get("preserve_sections")
    )
    final_bytes = final.encode()
    diff = difflib.unified_diff(
        (current or "").split("\n"),
        final.split("\n"),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        lineterm="",
    )
    safe_diff = "\n".join(_slack_safe_text(line) for line in diff)
    old = expected[:12] + "…" if expected else "(new)"
    return (
        f"Target: {rel}",
        f"Version: {old} → {hashlib.sha256(final_bytes).hexdigest()[:12]}… · "
        f"{len(current_bytes or b'')} → {len(final_bytes)} bytes",
        "Complete diff:\n" + (safe_diff or "(no content change)"),
    )


def _insert_line_review(payload: Mapping[str, Any]) -> tuple[str, ...]:
    from src.brain.actions.handlers.insert_vault_line import insert_after_anchor
    from src.brain.atomic_io import confined_path, read_regular_text
    from src.orchestrator.config import VAULT_PATH

    rel = validate_vault_note_path(payload.get("path"))
    try:
        current = read_regular_text(confined_path(VAULT_PATH, rel))
    except FileNotFoundError as exc:
        raise ValueError("vault target does not exist") from exc
    insertion = insert_after_anchor(current, payload["anchor"], payload["text"])
    if insertion.already_present:
        raise ValueError("the line is already present directly after the anchor")
    lines = [
        "Effect: Add one line; nothing else in the note changes",
        f"Target: {_slack_safe_text(rel)}",
    ]
    if insertion.section:
        lines.append(f"Section: {_slack_safe_text(insertion.section)}")
    lines.append(
        f"After line {insertion.anchor_line}: {_slack_safe_text(payload['anchor'])}"
    )
    lines.append(f"Add: {_slack_safe_text(payload['text'])}")
    return tuple(lines)


def payload_validation_error(
    action_type: ActionType, payload: dict[str, Any]
) -> str | None:
    """Validate the canonical action schema; unknown effects fail closed."""
    allowed = _ALLOWED_FIELDS.get(action_type, frozenset())
    unknown = sorted(set(payload) - allowed)
    if unknown:
        return f"queued payload has unsupported fields: {', '.join(unknown)}"
    for key in _REQUIRED_NONEMPTY.get(action_type, ()):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"queued payload requires non-empty {key}"

    nullable = _NULLABLE_FIELDS.get(action_type, frozenset())
    scalar_fields = (
        _ALLOWED_FIELDS.get(action_type, frozenset())
        - _LIST_FIELDS
        - _BOOLEAN_FIELDS
        - _INTEGER_FIELDS
        - {"changes", "expected_sha256", "preserve_sections", "new_content"}
    )
    for key in scalar_fields & payload.keys():
        value = payload[key]
        if value is None and key in nullable:
            continue
        if not isinstance(value, str):
            return f"queued payload field {key} must be a string"
    for key in _BOOLEAN_FIELDS & payload.keys():
        if not isinstance(payload[key], bool):
            return f"queued payload field {key} must be boolean"
    for key in _INTEGER_FIELDS & payload.keys():
        value = payload[key]
        if value is None and key in nullable:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            return f"queued payload field {key} must be an integer"
    for key in _LIST_FIELDS & payload.keys():
        value = payload[key]
        if value is None and key in nullable:
            continue
        if not isinstance(value, list):
            return f"queued payload field {key} must be a list"

    if action_type == ActionType.SEND_BOT_MESSAGE:
        try:
            normalize_bot_message_payload(payload)
        except ValueError as exc:
            return f"queued Brain bot message is invalid: {exc}"

    if action_type == ActionType.SEND_DRAFT:
        size = payload.get("draft_size")
        digest = payload.get("draft_sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return "queued draft requires a non-negative integer draft_size"
        if not is_sha256_hex(digest):
            return "queued draft requires a valid draft_sha256"

    if action_type == ActionType.PROMOTE_INTAKE:
        size = payload.get("intake_size")
        digest = payload.get("intake_sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return "queued intake promotion requires a non-negative intake_size"
        if not is_sha256_hex(digest):
            return "queued intake promotion requires a valid intake_sha256"

    priority = payload.get("priority")
    if priority is not None:
        if action_type == ActionType.CREATE_REMINDER and not 0 <= priority <= 9:
            return "queued reminder priority must be between 0 and 9"
        if action_type == ActionType.CREATE_TODOIST_TASK and not 1 <= priority <= 4:
            return "queued task priority must be between 1 and 4"

    alarm_minutes = payload.get("alarm_minutes_before")
    if alarm_minutes is not None and not 0 <= alarm_minutes <= 10080:
        return "queued alarm_minutes_before must be between 0 and 10080"

    if action_type == ActionType.EDIT_VAULT_FILE:
        try:
            validate_vault_note_path(payload.get("path"))
        except ValueError as exc:
            return f"queued vault edit has unsafe path: {exc}"
        if "expected_sha256" not in payload:
            return "queued vault edit is missing its approved version"
        if not isinstance(payload.get("new_content"), str):
            return "queued vault edit requires complete string new_content"
        try:
            validate_expected_file_sha256(payload.get("expected_sha256"))
        except ValueError as exc:
            return f"queued vault edit has invalid version: {exc}"
        preserve = payload.get("preserve_sections")
        if preserve is not None and (
            not isinstance(preserve, list)
            or any(not isinstance(item, str) or not item for item in preserve)
        ):
            return "queued vault edit has invalid preserve_sections"

    if action_type == ActionType.INSERT_VAULT_LINE:
        from src.brain.actions.handlers.insert_vault_line import validate_single_line

        try:
            validate_vault_note_path(payload.get("path"))
        except ValueError as exc:
            return f"queued line insertion has unsafe path: {exc}"
        try:
            validate_single_line(payload.get("anchor"), "anchor")
            validate_single_line(payload.get("text"), "text")
        except ValueError as exc:
            return f"queued line insertion is invalid: {exc}"

    if action_type == ActionType.EDIT_VAULT_FILES:
        changes = payload.get("changes")
        if not isinstance(changes, list) or not changes:
            return "queued vault edit requires a non-empty changes list"
        if len(changes) > MAX_VAULT_FILE_CHANGES:
            return (
                "queued vault edit exceeds the "
                f"{MAX_VAULT_FILE_CHANGES}-file review limit"
            )
        seen: set[str] = set()
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                return f"queued vault change {index} is not an object"
            unknown_change = sorted(
                set(change) - {
                    "path", "new_content", "preserve_sections", "expected_sha256",
                }
            )
            if unknown_change:
                return (
                    f"queued vault change {index} has unsupported fields: "
                    f"{', '.join(unknown_change)}"
                )
            path = change.get("path")
            try:
                validate_vault_note_path(path)
            except ValueError as exc:
                return f"queued vault change {index} has unsafe path: {exc}"
            if "expected_sha256" not in change:
                return f"queued vault change {index} is missing its approved version"
            assert isinstance(path, str)
            if path.casefold() in seen:
                return f"queued vault change {index} repeats path"
            seen.add(path.casefold())
            if not isinstance(change.get("new_content"), str):
                return f"queued vault change {index} requires complete new_content"
            try:
                validate_expected_file_sha256(change.get("expected_sha256"))
            except ValueError as exc:
                return f"queued vault change {index} has invalid version: {exc}"
            preserve = change.get("preserve_sections")
            if preserve is not None and (
                not isinstance(preserve, list)
                or any(not isinstance(item, str) or not item for item in preserve)
            ):
                return f"queued vault change {index} has invalid preserve_sections"

    string_list_fields = _LIST_FIELDS - {"emails", "tels", "messages"}
    for key in string_list_fields & payload.keys():
        values = payload[key]
        if values is not None and any(not isinstance(item, str) for item in values):
            return f"queued payload field {key} must contain only strings"

    for key in {"emails", "tels"} & payload.keys():
        values = payload[key]
        if values is None:
            continue
        for item in values:
            if isinstance(item, str):
                continue
            if not isinstance(item, dict) or set(item) - {"value", "type"}:
                return f"queued payload field {key} has an invalid contact value"
            if not isinstance(item.get("value"), str):
                return f"queued payload field {key} has an invalid contact value"
            kind = item.get("type")
            kinds = kind if isinstance(kind, list) else [kind]
            if kind is not None and not all(is_vcard_type_token(part) for part in kinds):
                return f"queued payload field {key} has an invalid contact type"

    proposal_ids = payload.get("proposal_action_ids")
    if proposal_ids is not None:
        normalized_ids = [item.lower() for item in proposal_ids]
        if len(proposal_ids) > 10 or len(set(normalized_ids)) != len(proposal_ids):
            return "queued notification has invalid attached approval ids"
        if any(
            len(item) != 12
            or any(char not in "0123456789abcdef" for char in item.lower())
            for item in proposal_ids
        ):
            return "queued notification has invalid attached approval ids"

    media_ids = payload.get("media_ids")
    if media_ids is not None:
        from src.brain.media.store import parse_media_ids

        try:
            valid = parse_media_ids(media_ids) == media_ids
        except ValueError:
            valid = False
        if not valid:
            return "queued notification has invalid media ids"

    occasion_fields = {"occasion_person_id", "occasion", "occasion_year"}
    present_occasion_fields = occasion_fields & payload.keys()
    if present_occasion_fields and present_occasion_fields != occasion_fields:
        return "queued occasion reminder requires person, occasion, and year together"
    if present_occasion_fields and (
        not str(payload["occasion_person_id"]).strip()
        or not str(payload["occasion"]).strip()
        or isinstance(payload["occasion_year"], bool)
    ):
        return "queued occasion reminder has an invalid identity"

    if action_type == ActionType.MAIL_ARCHIVE_BATCH:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_ARCHIVE_BATCH:
            return f"queued mail archive batch needs 1 to {MAX_ARCHIVE_BATCH} messages"
        for number, message in enumerate(messages, start=1):
            if not isinstance(message, dict):
                return f"queued mail archive batch message {number} must be an object"
            error = payload_validation_error(ActionType.MAIL_ARCHIVE, message)
            if error:
                return f"queued mail archive batch message {number}: {error}"

    if action_type == ActionType.MAIL_ARCHIVE:
        from src.brain.mcp.tools._icloud import (
            validate_mailbox_name,
            validate_single_uid,
        )

        if payload.get("provider") not in {"icloud", "gmail"}:
            return "queued mail archive has unsupported provider"
        try:
            validate_single_uid(cast(str, payload.get("uid")))
        except (TypeError, ValueError):
            return "queued mail archive requires a positive numeric uid"
        try:
            validate_mailbox_name(cast(str, payload.get("mailbox")))
        except (TypeError, ValueError) as exc:
            return f"queued mail archive has invalid mailbox: {exc}"
        snapshot_fields = {
            "uidvalidity", "message_id", "message_from", "message_subject",
            "message_header_sha256",
        }
        if not snapshot_fields <= payload.keys():
            return "queued mail archive is missing its approved message snapshot"
        try:
            validate_single_uid(cast(str, payload.get("uidvalidity")))
        except (TypeError, ValueError):
            return "queued mail archive has invalid UIDVALIDITY"
        digest = payload.get("message_header_sha256")
        if (
            not isinstance(digest, str)
            or not is_sha256_hex(digest)
            or digest != digest.lower()
        ):
            return "queued mail archive has invalid header snapshot"

    action_note_id = payload.get("action_note_id")
    if action_note_id is not None and (
        len(action_note_id) != 13
        or not action_note_id.startswith("A-")
        or action_note_id[10] != "-"
        or not (action_note_id[2:10] + action_note_id[11:]).isdigit()
    ):
        return "action_note_id must match A-YYYYMMDD-NN"

    if action_type == ActionType.UPDATE_CALENDAR_EVENT and not any(
        key in payload
        for key in ("summary", "start", "end", "location", "description", "cancelled")
    ):
        return "queued calendar update has no fields to change"
    if (
        action_type == ActionType.CREATE_TODOIST_TASK
        and payload.get("due_date")
        and payload.get("due_string")
    ):
        return "queued task cannot combine due_date and due_string"
    if action_type == ActionType.UPDATE_CONTACT:
        for left, right in (
            ("set_emails", "add_emails"),
            ("set_tels", "add_tels"),
            ("note", "note_append"),
        ):
            if left in payload and right in payload:
                return f"queued contact update cannot combine {left} and {right}"
        if not any(key != "href" for key in payload):
            return "queued contact update has no fields to change"
    return None


_RELAY = re.compile(r"^(?P<user>.+?)_at_(?P<domain>[a-z0-9_]+?)_[a-z0-9]{6,}_[a-z0-9]+@icloud\.com$", re.IGNORECASE)
_ADDRESS = re.compile(r"<([^<>]+)>\s*$")


def sender_label(from_header: str) -> str:
    """A short name for a mail's sender: the site behind an iCloud relay, else the name, else the domain."""
    match = _ADDRESS.search(from_header)
    address = match.group(1).strip() if match else from_header.strip()
    relay = _RELAY.match(address)
    if relay:
        return relay.group("domain").replace("_", ".").lower()
    name = from_header[: match.start()].strip().strip('"') if match else ""
    if name:
        return name
    return address.rpartition("@")[2] or address


def _archive_batch_lines(payload: Mapping[str, Any]) -> tuple[str, ...]:
    senders: dict[str, list[str]] = {}
    for message in payload["messages"]:
        senders.setdefault(sender_label(str(message["message_from"])), []).append(str(message["message_subject"]))
    ranked = sorted(senders.items(), key=lambda item: -len(item[1]))
    lines = []
    for sender, subjects in ranked[:_BATCH_SENDERS_SHOWN]:
        lines.append(f"• {len(subjects)} from {_summary_value(sender)}")
        lines.append(f"   {_text_preview(subjects[0], limit=80)}")
    if len(ranked) > _BATCH_SENDERS_SHOWN:
        rest = sum(len(subjects) for _sender, subjects in ranked[_BATCH_SENDERS_SHOWN:])
        lines.append(f"• {rest} more from {len(ranked) - _BATCH_SENDERS_SHOWN} other senders")
    return tuple(lines)


def _summary_lines(action_type: ActionType, payload: dict[str, Any]) -> tuple[str, ...]:
    if action_type == ActionType.SEND_DRAFT:
        from src.brain.actions.handlers._draft_io import (
            extract_body,
            frontmatter_field,
            read_draft_file,
        )
        from src.orchestrator.config import VAULT_PATH

        _path, data, _digest = read_draft_file(
            VAULT_PATH,
            payload["draft_path"],
            expected_sha256=payload["draft_sha256"],
            expected_size=payload["draft_size"],
        )
        text = data.decode("utf-8")
        kind = frontmatter_field(text, "kind") or "doc"
        title = frontmatter_field(text, "title") or "(untitled)"
        recipient = (
            payload.get("area")
            or frontmatter_field(text, "recipient")
            or frontmatter_field(text, "area")
            or "local draft archive"
        )
        return (
            f"Title: {_display_value(title)}",
            f"Effect: {'Send' if kind == 'message' else 'Archive'} {kind} "
            f"to {_display_value(recipient)}",
            f"Complete body: {_display_value(extract_body(text))}",
        )

    if action_type == ActionType.CREATE_TODOIST_TASK:
        lines = [f"Task: {_display_value(payload['content'])}"]
        if payload.get("description"):
            lines.append(f"Description: {_display_value(payload['description'])}")
        lines.append(f"List: {_display_value(payload['project_id'])}")
        due = payload.get("due_date") or payload.get("due_string")
        if due:
            lines.append(f"Due: {_display_value(due)}")
        if payload.get("priority") is not None:
            lines.append(f"Priority: {payload['priority']}")
        if payload.get("labels"):
            lines.append(f"Labels: {_display_value(payload['labels'])}")
        if payload.get("action_note_id"):
            lines.append(f"Action note: {payload['action_note_id']}")
        return tuple(lines)

    if action_type == ActionType.EDIT_VAULT_FILE:
        return ("Effect: Apply the exact complete diff below", *_vault_change_lines(payload))

    if action_type == ActionType.MAIL_ARCHIVE_BATCH:
        return _archive_batch_lines(payload)

    if action_type == ActionType.INSERT_VAULT_LINE:
        return _insert_line_review(payload)

    if action_type == ActionType.EDIT_VAULT_FILES:
        changes = payload.get("changes")
        if isinstance(changes, list):
            lines = []
            if payload.get("summary"):
                lines.append(f"Summary: {_text_preview(payload['summary'])}")
            lines.append(f"Effect: Apply {len(changes)} exact complete diff(s) below")
            for change in changes:
                lines.extend(_vault_change_lines(change))
            return tuple(lines)

    if action_type == ActionType.PROMOTE_INTAKE:
        from src.brain.actions.handlers.promote_intake import intake_snapshot
        from src.db import get_db

        row = get_db().execute(
            "SELECT content, source, source_id FROM intake "
            "WHERE id=? AND status='pending'",
            (payload["intake_id"],),
        ).fetchone()
        if row is None:
            raise ValueError("intake row is missing or no longer pending")
        content, source, source_id = map(str, row)
        if any(
            payload.get(key) != value
            for key, value in intake_snapshot(content, source, source_id).items()
        ):
            raise ValueError("intake content changed after proposal")
        return (
            f"Capture: {_display_value(payload['intake_id'])}",
            f"Source: {_display_value(source)} / {_display_value(source_id)}",
            f"Complete content: {_display_value(content)}",
            "Effect: Publish these frozen intake bytes as a vault note",
        )

    lines = [
        f"{label}: {_summary_value(sender_label(str(payload[key])) if key == 'message_from' else payload[key])}"
        for key, label in summary_fields(action_type)
        if key in payload and payload[key] is not None
    ]
    if action_type == ActionType.PUSH_NOTIFICATION and payload.get(
        "occasion_person_id"
    ):
        lines.append(
            "Effect: after complete Slack delivery, suppress this exact occasion "
            "reminder for the recorded year"
        )
    return tuple(lines)


def build_approval_review(row: Mapping[str, Any]) -> ApprovalReview:
    raw = row.get("payload_json")
    if not isinstance(raw, str):
        return ApprovalReview(False, reason="queued payload is missing")

    expected_hash = row.get("payload_hash")
    actual_hash = payload_hash(raw)
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or not hmac.compare_digest(expected_hash.lower(), actual_hash)
    ):
        return ApprovalReview(False, reason="queued payload failed its integrity check")

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return ApprovalReview(False, reason="queued payload is malformed")
    if not isinstance(payload, dict):
        return ApprovalReview(False, reason="queued payload is not an object")

    try:
        action_type = ActionType(str(row.get("type") or ""))
    except ValueError:
        return ApprovalReview(False, reason="queued action type is unsupported")
    heading = _ACTION_LABELS.get(action_type)
    if heading is None:
        return ApprovalReview(False, reason="queued action type is unsupported")

    origin_area = row.get("origin_area")
    origin_agent = row.get("origin_agent")
    if origin_area and "area" in payload and payload["area"] != origin_area:
        return ApprovalReview(False, reason="queued area does not match action origin")
    for key in ("agent", "proposing_agent"):
        if origin_agent and key in payload and payload[key] != origin_agent:
            return ApprovalReview(False, reason="queued agent does not match action origin")

    error = payload_validation_error(action_type, payload)
    if error:
        return ApprovalReview(False, reason=error)
    try:
        summary_lines = _summary_lines(action_type, payload)
    except (OSError, ValueError) as exc:
        return ApprovalReview(False, reason=f"queued effect is not reviewable: {exc}")
    if len("\n".join(summary_lines)) > MAX_SUMMARY_CHARS:
        return ApprovalReview(False, reason="queued effect summary is too large for Slack")

    raw_purpose = (
        payload.get("summary")
        if action_type == ActionType.EDIT_VAULT_FILES and payload.get("summary")
        else row.get("rationale")
    )
    purpose = _text_preview(raw_purpose, limit=MAX_PURPOSE_CHARS) if raw_purpose else ""
    if action_type == ActionType.EDIT_VAULT_FILES:
        changes = payload.get("changes")
        if isinstance(changes, list):
            heading = f"Update {len(changes)} note{'s' if len(changes) != 1 else ''}"

    title, note = heading, ""
    if action_type == ActionType.MAIL_ARCHIVE_BATCH:
        count = len(payload["messages"])
        title, note = f"Archive {count} email{'s' if count != 1 else ''}", "Moved out of the inbox, not deleted"
    elif action_type == ActionType.MAIL_ARCHIVE:
        title, note = "Archive 1 email", "Moved out of the inbox, not deleted"
    return ApprovalReview(
        True,
        heading=heading,
        purpose=purpose,
        summary_lines=summary_lines,
        payload_sha256=actual_hash,
        title=title,
        note=note,
    )
