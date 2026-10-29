from __future__ import annotations

from pathlib import Path

import yaml

from src.brain.plugins.base import Notifier, TaskManager
from src.brain.plugins.loader import load_notifier, load_tasks
from src.brain.plugins.notifiers.noop import NoopNotifier
from src.brain.plugins.notifiers.slack import SlackNotifier
from src.brain.plugins.tasks.apple_reminders import AppleRemindersTaskManager
from src.brain.plugins.tasks.noop import NoopTaskManager
from src.brain.plugins.tasks.todoist import TodoistTaskManager

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_noops_conform_to_protocols() -> None:
    assert isinstance(NoopNotifier(), Notifier)
    assert isinstance(NoopTaskManager(), TaskManager)


def test_real_impls_conform_to_protocols() -> None:
    assert isinstance(SlackNotifier(), Notifier)
    assert isinstance(TodoistTaskManager(), TaskManager)


def test_loader_resolves_current_integrations_yaml() -> None:
    integrations = yaml.safe_load((REPO_ROOT / "config" / "integrations.yaml").read_text()) or {}
    notifier = load_notifier(integrations)
    tasks = load_tasks(integrations)
    def _declared(key: str):
        value = integrations.get(key)
        return value.get("type") if isinstance(value, dict) else value

    notifier_classes = {"slack": SlackNotifier, "noop": NoopNotifier}
    task_classes = {
        "apple_reminders": AppleRemindersTaskManager,
        "todoist": TodoistTaskManager,
        "noop": NoopTaskManager,
    }
    declared_notifier = _declared("notifier")
    declared_tasks = _declared("tasks")
    assert isinstance(notifier, notifier_classes[declared_notifier]), (
        f"integrations.yaml declares notifier={declared_notifier!r}; "
        f"loader returned {type(notifier)}"
    )
    assert isinstance(tasks, task_classes[declared_tasks]), (
        f"integrations.yaml declares tasks={declared_tasks!r}; "
        f"loader returned {type(tasks)}"
    )


def test_loader_falls_back_to_noop_on_missing_key() -> None:
    assert isinstance(load_notifier({}), NoopNotifier)
    assert isinstance(load_tasks({}), NoopTaskManager)


def test_explicit_noop_selection() -> None:
    cfg = {"notifier": "noop", "tasks": "noop"}
    assert isinstance(load_notifier(cfg), NoopNotifier)
    assert isinstance(load_tasks(cfg), NoopTaskManager)


def test_dict_with_options_shape() -> None:
    cfg = {"notifier": {"type": "noop"}}
    assert isinstance(load_notifier(cfg), NoopNotifier)
