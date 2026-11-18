"""Write one change log note per day from the audit log and the nightly backup.

``python -m src.brain.connector.change_log`` writes ``Change Log/YYYY-MM-DD.md``
for yesterday (or for ``--day``). The note has two parts. **Through Friday** is
a table of every successful write the connector made that day, with the local
time, the person and the kind of change; it comes from the audit log. **Changed
outside Friday** lists the notes the nightly backup shows as changed that day
that no connector write explains. A change typed in Obsidian or made by a Claude
Code session cannot be tied to a person, because iCloud does not record who made
it, so it appears only in the second part.

A local day runs from midnight to midnight in the connector's time zone, so a
day can sit in two monthly audit files. The audit reader accepts every shape the
log has had: older lines lack ``person`` and ``result_id`` and carry ``args`` as
a string that may be cut off, so only ``time``, ``tool``, ``target``, ``result``,
``person`` and ``result_id`` are read, and a line that is not JSON, or has no
usable ``time`` or ``tool``, is skipped. Lines from before ``person`` existed
belong to the person with the empty address prefix.

If the note already exists the script does nothing, so a second run never
overwrites it. A missing or unreadable backup repository gives an empty second
part, never an error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import posixpath
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from src.brain.connector.config import load_config
from src.brain.connector.layout import DEFAULT_LAYOUT, VaultLayout
from src.brain.connector.people import DEFAULT_PERSON_KEY, Person
from src.brain.connector.server import AUDIT_DIR
from src.brain.connector.vault.coordination import StaleVersion, coordinated_write, on_disk_path
from src.brain.connector.vault.paths import PathRefused, check_writable

_logger = logging.getLogger(__name__)

WRITE_TOOLS = frozenset(
    {
        "vault_append",
        "vault_create",
        "vault_edit",
        "vault_edit_confirm",
        "vault_undo",
        "open_item_add",
    }
)

CHANGE_LOG_DIR = "Change Log"
DEFAULT_BACKUP_REPO = "~/brain-vault.git"
LOOK_BACK_DAYS = 90
NOT_IN_LOG = "(note not in the log)"
ACTIONS_PREFIX = "Actions: "
_GIT_TIMEOUT_SECONDS = 60

_WORDING = {
    "vault_append": "added to",
    "vault_create": "created",
    "vault_edit": "edited",
    "vault_edit_confirm": "edited after approval",
    "vault_undo": "undid an edit in",
    "open_item_add": "open item",
}
_NO_CHANGES = "Nothing was changed through Friday on this day."
_NO_OUTSIDE = "No other changed notes were found in the nightly backup."


@dataclass(frozen=True)
class Change:
    """One successful write: the local time (HH:MM), the person's key, the tool and the note."""

    time: str
    person: str
    tool: str
    note: str


@dataclass(frozen=True)
class _Entry:
    when: datetime
    tool: str
    target: str
    result: str
    person: str
    result_id: str


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _entry(raw: str) -> _Entry | None:
    """One audit line as an entry, or None when it is not a usable JSON object."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    tool = _text(data.get("tool"))
    stamp = _text(data.get("time"))
    if not tool or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo("UTC"))
    return _Entry(
        when=when,
        tool=tool,
        target=_text(data.get("target")),
        result=_text(data.get("result")),
        person=_text(data.get("person")),
        result_id=_text(data.get("result_id")),
    )


def _months(first: datetime, last: datetime) -> list[str]:
    utc = ZoneInfo("UTC")
    start, end = first.astimezone(utc), last.astimezone(utc)
    names = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        names.append(f"audit-{year:04d}-{month:02d}.jsonl")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return names


def _read_entries(audit_dir: Path, first: datetime, last: datetime) -> list[_Entry]:
    entries: list[_Entry] = []
    for name in _months(first, last):
        try:
            text = (audit_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            entry = _entry(raw) if raw.strip() else None
            if entry is not None and first <= entry.when < last:
                entries.append(entry)
    entries.sort(key=lambda entry: entry.when)
    return entries


def _clean_path(path: str) -> str:
    return unicodedata.normalize("NFC", posixpath.normpath(path.strip())).lstrip("/")


def _root_person(people: Sequence[Person]) -> Person | None:
    return next((person for person in people if not person.prefix), None)


def _root_key(people: Sequence[Person]) -> str:
    root = _root_person(people)
    return root.key if root is not None else DEFAULT_PERSON_KEY


def _layout_of(person: str, people: Sequence[Person]) -> VaultLayout:
    chosen = next((p for p in people if p.key == person), None) or _root_person(people)
    return chosen.layout if chosen is not None else DEFAULT_LAYOUT


def _note_of(
    entries: Sequence[_Entry],
    index: int,
    zone: tzinfo,
    people: Sequence[Person],
    depth: int = 0,
) -> str:
    entry = entries[index]
    if entry.tool == "open_item_add":
        return ACTIONS_PREFIX + entry.target
    if entry.tool in ("vault_edit_confirm", "vault_undo"):
        if depth >= 3 or not entry.target:
            return NOT_IN_LOG
        for earlier in range(index - 1, -1, -1):
            if entries[earlier].result_id == entry.target:
                return _note_of(entries, earlier, zone, people, depth + 1)
        return NOT_IN_LOG
    if entry.target.strip():
        return _clean_path(entry.target)
    if entry.tool == "vault_append":
        person = entry.person or _root_key(people)
        layout = _layout_of(person, people)
        return _clean_path(layout.daily_note_path(entry.when.astimezone(zone)))
    return NOT_IN_LOG


def _bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start, end


def through_friday(
    audit_dir: Path, day: date, zone: ZoneInfo, *, people: Sequence[Person] = ()
) -> list[Change]:
    """The successful writes of local ``day``, oldest first, from the audit files in ``audit_dir``.

    ``people`` gives each person's daily note layout, for a ``vault_append`` that named no
    note. Lines before people existed, and lines with no person, belong to the person with the
    empty address prefix.
    """
    start, end = _bounds(day, zone)
    entries = _read_entries(audit_dir, start - timedelta(days=LOOK_BACK_DAYS), end)
    root = _root_key(people)
    changes = []
    for index, entry in enumerate(entries):
        if not start <= entry.when < end:
            continue
        if entry.tool not in WRITE_TOOLS or entry.result != "ok":
            continue
        changes.append(
            Change(
                time=entry.when.astimezone(zone).strftime("%H:%M"),
                person=entry.person or root,
                tool=entry.tool,
                note=_note_of(entries, index, zone, people),
            )
        )
    return changes


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    path = os.environ.get("PATH", "/usr/bin:/bin")
    git = shutil.which("git", path=path)
    if git is None:
        _logger.warning("git was not found; the backup part of the change log is empty")
        return None
    env = {"PATH": path, "HOME": os.environ.get("HOME", "")}
    try:
        return subprocess.run(  # noqa: S603
            [git, f"--git-dir={repo}", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _logger.warning("git failed on the backup repository: %s", error)
        return None


def _commit_before(repo: Path, moment: datetime) -> str:
    """The newest commit made before ``moment``, or an empty string."""
    stamp = f"{int(moment.timestamp()) - 1} +0000"
    done = _git(repo, "rev-list", "-1", f"--before={stamp}", "HEAD")
    if done is None or done.returncode != 0:
        return ""
    return done.stdout.strip()


def _hidden(path: str) -> bool:
    return any(part.startswith(".") for part in path.split("/"))


def outside_friday(
    backup_repo: Path,
    day: date,
    friday_notes: Collection[str],
    *,
    zone: tzinfo | None = None,
    open_items_dir: str = "",
    open_items_made: int = 0,
) -> list[str]:
    """Paths that differ between the last backup before ``day`` and the last backup of ``day``.

    ``friday_notes`` (notes changed through Friday), the Change Log folder and anything under a
    path part that starts with a dot are left out. An open item gets a file name the audit log
    does not hold, so up to ``open_items_made`` files newly added under ``open_items_dir`` are
    taken to be those. The result is empty when the repository, either commit or git itself is
    missing, and never an error.
    """
    if not backup_repo.is_dir():
        return []
    start, end = _bounds(day, zone or datetime.now().astimezone().tzinfo or ZoneInfo("UTC"))
    before = _commit_before(backup_repo, start)
    last = _commit_before(backup_repo, end)
    if not before or not last or before == last:
        return []
    done = _git(backup_repo, "diff", "--name-status", "-z", "--no-renames", before, last)
    if done is None or done.returncode != 0:
        return []
    fields = done.stdout.split("\0")
    known = {_clean_path(note) for note in friday_notes}
    folder = _clean_path(open_items_dir) + "/" if open_items_dir else ""
    prefix = f"{CHANGE_LOG_DIR}/"
    found: list[tuple[str, bool]] = []
    for status, raw in zip(fields[0::2], fields[1::2], strict=False):
        path = unicodedata.normalize("NFC", raw)
        if not path or path.startswith(prefix) or _hidden(path) or path in known:
            continue
        found.append((path, status == "A"))
    result = []
    allowance = open_items_made
    for path, added in sorted(found):
        if added and folder and path.startswith(folder) and allowance > 0:
            allowance -= 1
            continue
        result.append(path)
    return result


def _cell(text: str) -> str:
    return " ".join(text.split("\n")).replace("|", "\\|")


def _link(path: str) -> str:
    return "[[" + (path.removesuffix(".md")) + "]]"


def _note_cell(change: Change) -> str:
    if change.tool == "open_item_add" or change.note == NOT_IN_LOG:
        return _cell(change.note)
    return _cell(_link(change.note))


def render(
    day: date, changes: Sequence[Change], outside: Sequence[str], names: Mapping[str, str]
) -> str:
    """The note for ``day``: frontmatter, the table of writes through Friday, the outside list."""
    lines = [
        "---",
        "type: change-log",
        f"date: {day.isoformat()}",
        "---",
        "",
        f"# Change log {day.isoformat()}",
        "",
        "## Through Friday",
        "",
    ]
    if changes:
        lines += ["| Time | Person | Change | Note |", "| --- | --- | --- | --- |"]
        for change in changes:
            person = names.get(change.person, change.person)
            what = _WORDING.get(change.tool, change.tool)
            lines.append(
                f"| {_cell(change.time)} | {_cell(person)} | {_cell(what)} | {_note_cell(change)} |"
            )
    else:
        lines.append(_NO_CHANGES)
    lines += ["", "## Changed outside Friday", ""]
    if outside:
        lines += [f"- {_link(path)}" for path in outside]
    else:
        lines.append(_NO_OUTSIDE)
    return "\n".join(lines) + "\n"


def _today(zone: tzinfo) -> date:
    return datetime.now(zone).date()


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a day written YYYY-MM-DD") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.brain.connector.change_log")
    parser.add_argument("--day", type=_parse_day, help="the local day, YYYY-MM-DD (default: yesterday)")
    parser.add_argument(
        "--backup-repo",
        type=Path,
        default=Path(DEFAULT_BACKUP_REPO).expanduser(),
        help="the Git repository the nightly backup commits to",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config()
        zone = ZoneInfo(config.timezone)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"change log: the connector settings could not be read: {error}", file=sys.stderr)
        return 1
    day = args.day or _today(zone) - timedelta(days=1)
    try:
        note = check_writable(config.vault_path, f"{CHANGE_LOG_DIR}/{day.isoformat()}.md")
    except PathRefused as error:
        print(f"change log: {error}", file=sys.stderr)
        return 1
    if on_disk_path(note).exists():
        return 0
    changes = through_friday(config.data_dir / AUDIT_DIR, day, zone, people=config.people)
    friday_notes = {
        change.note
        for change in changes
        if change.tool != "open_item_add" and change.note != NOT_IN_LOG
    }
    made = sum(1 for change in changes if change.tool == "open_item_add")
    outside = outside_friday(
        args.backup_repo,
        day,
        friday_notes,
        zone=zone,
        open_items_dir=config.layout.actions_dir,
        open_items_made=made,
    )
    names = {person.key: person.name for person in config.people}
    text = render(day, changes, outside, names)
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
        coordinated_write(note, text, None)
    except StaleVersion:
        return 0  # the note appeared while this run was working; it stays as it is
    except OSError as error:
        print(f"change log: the note was not written: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
