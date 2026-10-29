"""Open items in the vault's actions folder (``Actions/`` by default).

``open_item_add`` is the plain function behind the ``open_item_add`` tool. It
creates one note in ``Actions/`` for one thing that is left open, following the
owner's Actions trial: the note has the frontmatter that the ``Actions.base``
view filters on, a heading with the outcome, and a ``Source:`` line.

The file name is ``A-YYYYMMDD-NN - <Outcome>.md``. ``NN`` is the next free
number for that day, found from the file names already in ``Actions/``. In the
file name, ``/`` and ``\\`` become ``-`` and the characters ``: * ? " < > |``
are dropped; the heading keeps the outcome as written. An item that is
``waiting`` needs both who it waits on and the date to follow up. For a
``next`` item those two are left out.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from src.brain.connector.layout import DEFAULT_LAYOUT, VaultLayout
from src.brain.connector.vault.read import ReadResult
from src.brain.connector.vault.write import ICLOUD_SUFFIX, create

_DROPPED = str.maketrans("", "", ':*?"<>|')
_SEPARATORS = str.maketrans({"/": "-", "\\": "-"})
_NUMBERED = re.compile(r"^A-(\d{8})-(\d{2})(?: |\.|$)")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _quoted(text: str) -> str:
    escaped = _one_line(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _file_name_part(outcome: str) -> str:
    return _one_line(outcome.translate(_SEPARATORS).translate(_DROPPED))


def _next_number(vault: Path, day: str, folder_name: str) -> int:
    folder = vault.expanduser() / folder_name
    if not folder.is_dir():
        return 1
    highest = 0
    for entry in folder.iterdir():
        name = entry.name
        if name.startswith(".") and name.endswith(ICLOUD_SUFFIX):
            name = name[1 : -len(ICLOUD_SUFFIX)]
        match = _NUMBERED.match(name)
        if match and match.group(1) == day:
            highest = max(highest, int(match.group(2)))
    return highest + 1


def open_item_add(
    vault: Path,
    outcome: str,
    status: Literal["next", "waiting"],
    due: date | None = None,
    waiting_on: str | None = None,
    follow_up: date | None = None,
    source: str = "",
    today: date | None = None,
    *,
    layout: VaultLayout = DEFAULT_LAYOUT,
) -> ReadResult:
    """Create one note in the actions folder and return it as read back.

    Raises ``ValueError`` for an empty outcome, an unknown status, a ``waiting`` item
    without ``waiting_on`` and ``follow_up``, or more than 99 items in a day.
    """
    heading = _one_line(outcome)
    name_part = _file_name_part(outcome)
    if not heading or not name_part:
        raise ValueError("the outcome is empty")
    if status not in ("next", "waiting"):
        raise ValueError(f"status must be next or waiting, not {status!r}")
    waiting_on = _one_line(waiting_on or "")
    if status == "waiting" and (not waiting_on or follow_up is None):
        raise ValueError("a waiting item needs waiting_on and follow_up")
    day = today if today is not None else datetime.now().astimezone().date()
    stamp = f"{day:%Y%m%d}"
    number = _next_number(vault, stamp, layout.actions_dir)
    if number > 99:
        raise ValueError(f"there are already 99 open items for {day.isoformat()}")
    action_id = f"A-{stamp}-{number:02d}"
    source = _one_line(source)

    lines = ["---", "type: action", f"action_id: {action_id}", f"status: {status}"]
    lines.append("priority: normal")
    if due is not None:
        lines.append(f"due: {due.isoformat()}")
    if status == "waiting" and follow_up is not None:
        lines.append(f"waiting_on: {_quoted(waiting_on)}")
        lines.append(f"follow_up: {follow_up.isoformat()}")
    lines += [f"added: {day.isoformat()}", f"updated: {day.isoformat()}"]
    if source:
        lines.append(f"source: {_quoted(source)}")
    lines += ["tags: [action]", "---", "", f"# {heading}"]
    if source:
        lines += ["", f"Source: {source}"]
    return create(
        vault, f"{layout.actions_dir}/{action_id} - {name_part}.md", "\n".join(lines) + "\n", layout=layout
    )
