"""The old name of ``integrations.apple``, kept for one release; import that module instead."""

from src.brain.connector.integrations.apple import (
    DEFAULT_ALERT_MINUTES,
    ICLOUD_ONLY,
    OTHERS_REFUSED,
    ICloudCall,
    brain_schema,
    calendar_create,
    calendar_update,
    icloud_bridge,
    local_time,
    mail_archive,
    reminder_complete,
    reminder_create,
    reminder_update,
)

__all__ = [
    "DEFAULT_ALERT_MINUTES",
    "ICLOUD_ONLY",
    "OTHERS_REFUSED",
    "ICloudCall",
    "brain_schema",
    "calendar_create",
    "calendar_update",
    "icloud_bridge",
    "local_time",
    "mail_archive",
    "reminder_complete",
    "reminder_create",
    "reminder_update",
]
