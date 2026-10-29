from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.brain.actions.handlers import (
    complete_reminder,
    create_calendar_event,
    create_contact,
    create_reminder,
    create_todoist_task,
    edit_vault_file,
    edit_vault_files,
    insert_vault_line,
    mail_archive,
    mail_archive_batch,
    promote_intake,
    promote_note,
    push_notification,
    send_bot_message,
    send_draft,
    update_calendar_event,
    update_contact,
)
from src.brain.actions.types import ActionType

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

HANDLERS: dict[ActionType, Handler] = {
    ActionType.PUSH_NOTIFICATION: push_notification.execute,
    ActionType.SEND_BOT_MESSAGE: send_bot_message.execute,
    ActionType.SEND_DRAFT: send_draft.execute,
    ActionType.CREATE_TODOIST_TASK: create_todoist_task.execute,
    ActionType.EDIT_VAULT_FILE: edit_vault_file.execute,
    ActionType.EDIT_VAULT_FILES: edit_vault_files.execute,
    ActionType.CREATE_CALENDAR_EVENT: create_calendar_event.execute,
    ActionType.UPDATE_CALENDAR_EVENT: update_calendar_event.execute,
    ActionType.MAIL_ARCHIVE: mail_archive.execute,
    ActionType.MAIL_ARCHIVE_BATCH: mail_archive_batch.execute,
    ActionType.CREATE_REMINDER: create_reminder.execute,
    ActionType.COMPLETE_REMINDER: complete_reminder.execute,
    ActionType.CREATE_CONTACT: create_contact.execute,
    ActionType.UPDATE_CONTACT: update_contact.execute,
    ActionType.PROMOTE_INTAKE: promote_intake.execute,
    ActionType.PROMOTE_NOTE: promote_note.execute,
    ActionType.INSERT_VAULT_LINE: insert_vault_line.execute,
}
