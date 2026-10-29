from __future__ import annotations

import importlib
import os
from typing import Any

from src.brain.plugins.base import Notifier, ReplyListener, TaskManager

_BUILTIN_NOTIFIERS = {
    "noop": "src.brain.plugins.notifiers.noop:NoopNotifier",
    "slack": "src.brain.plugins.notifiers.slack:SlackNotifier",
}

_BUILTIN_LISTENERS = {
    "slack": "src.brain.plugins.listeners.slack:SlackListener",
}

_BUILTIN_TASKS = {
    "noop": "src.brain.plugins.tasks.noop:NoopTaskManager",
    "todoist": "src.brain.plugins.tasks.todoist:TodoistTaskManager",
    "apple_reminders": "src.brain.plugins.tasks.apple_reminders:AppleRemindersTaskManager",
}

def _slack_off() -> bool:
    return os.getenv("BRAIN_SLACK_OFF", "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve(spec: str) -> type:
    module_path, _, class_name = spec.rpartition(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _parse_choice(value: Any) -> tuple[str, dict]:
    if isinstance(value, str):
        return value, {}
    if isinstance(value, dict):
        return value["type"], dict(value.get("options", {}))
    raise TypeError(f"integrations.yaml value must be str or dict, got {type(value).__name__}")


def load_notifier(integrations: dict, fallback: str = "noop") -> Notifier:
    name, opts = _parse_choice(integrations.get("notifier", fallback))
    if name == "slack" and _slack_off():
        name, opts = "noop", {}
    cls = _resolve(_BUILTIN_NOTIFIERS[name])
    return cls(**opts)


def load_listener(integrations: dict) -> ReplyListener | None:
    """Resolve the reply listener from the configured notifier type.

    By convention the listener plugin has the same short name as the notifier.
    Returns None if no listener is registered for that notifier (e.g. 'noop').
    The listener also accepts the notifier.options dict so it can read
    authorized_senders/channels without a separate config section.
    """
    name, opts = _parse_choice(integrations.get("notifier", "noop"))
    if name == "slack" and _slack_off():
        return None
    spec = _BUILTIN_LISTENERS.get(name)
    if not spec:
        return None
    cls = _resolve(spec)
    return cls(**opts)


def load_tasks(integrations: dict, fallback: str = "noop") -> TaskManager:
    name, opts = _parse_choice(integrations.get("tasks", fallback))
    cls = _resolve(_BUILTIN_TASKS[name])
    return cls(**opts)
