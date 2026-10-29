from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.brain.atomic_io import atomic_write_text, ensure_real_directory, read_regular_text
from src.brain.followups.entries import Entry
from src.brain.followups.scrub import scrub_secrets

_TRUNCATED = " […]"
_KEEP_DIGESTS = 7
_FOLDER_MODE = 0o700
_FILE_MODE = 0o600
_STATE_FILE = "state.json"
_DIGEST_NAME = re.compile(r"digest-\d{4}-\d{2}-\d{2}\.md")
_INDEX_NAME = re.compile(r"index-\d{4}-\d{2}-\d{2}\.json")
_LINE_BREAKS = re.compile(r"\r\n?|[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_HEADING_START = re.compile(r"(?m)^([ \t\u00a0\u200b-\u200f\u2060-\u2064\ufeff]*)#")
_ENTRY_START = re.compile(r"\n(?=### )")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_ASSISTANTS = frozenset({"claude", "alfred"})
_THREAD_CHARS = 100


def render_digest(
    entries: Sequence[Entry],
    *,
    max_bytes: int = 200_000,
    max_message_chars: int = 2_000,
    max_assistant_chars: int = 800,
) -> str:
    blocks: list[str] = []
    size = 0
    for entry in sorted(entries, key=lambda item: _moment(item.when), reverse=True):
        limit = max_assistant_chars if entry.who in _ASSISTANTS else max_message_chars
        block = _block(entry, limit)
        added = len(block.encode("utf-8")) + (1 if blocks else 0)
        if size + added > max_bytes:
            break
        blocks.append(block)
        size += added
    return "\n".join(blocks)


def split_digest(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for block in _ENTRY_START.split(text):
        if len(block) > max_chars:
            block = block[: max_chars - len(_TRUNCATED)] + _TRUNCATED
        if current and len(current) + 1 + len(block) > max_chars:
            parts.append(current)
            current = block
        else:
            current = f"{current}\n{block}" if current else block
    if current:
        parts.append(current)
    return parts


def digest_path(folder: Path, day: date) -> Path:
    return folder / f"digest-{day.isoformat()}.md"


def write_digest(folder: Path, text: str, *, today: date) -> Path:
    return _write_dated(folder, digest_path(folder, today).name, text, _DIGEST_NAME)


def write_index(folder: Path, refs: Mapping[str, Mapping[str, str]], *, today: date) -> Path:
    text = json.dumps(refs, indent=1, sort_keys=True) + "\n"
    return _write_dated(folder, f"index-{today.isoformat()}.json", text, _INDEX_NAME)


def read_index(folder: Path, *, today: date) -> dict[str, dict[str, str]]:
    try:
        refs = json.loads(read_regular_text(folder / f"index-{today.isoformat()}.json"))
    except (OSError, ValueError):
        return {}
    if not isinstance(refs, dict):
        return {}
    return {
        ref: {"source": placed["source"], "place": placed["place"]}
        for ref, placed in refs.items()
        if isinstance(placed, dict)
        and isinstance(placed.get("source"), str)
        and isinstance(placed.get("place"), str)
    }


def load_state(folder: Path) -> dict[str, Any]:
    try:
        state = json.loads(read_regular_text(folder / _STATE_FILE))
    except (FileNotFoundError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(folder: Path, state: dict[str, Any]) -> None:
    _private_folder(folder)
    atomic_write_text(
        folder / _STATE_FILE,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        mode=_FILE_MODE,
        preserve_existing_mode=False,
    )


def promote_cursor(user_dir: Path) -> bool:
    folder = user_dir / "followups"
    pending = load_state(folder).get("pending")
    if not isinstance(pending, dict):
        return False
    save_state(folder, {"committed": pending})
    return True


def _block(entry: Entry, max_chars: int) -> str:
    text = _HEADING_START.sub(r"\1\\#", scrub_secrets(_LINE_BREAKS.sub("\n", entry.text)))
    if len(text) > max_chars:
        text = text[:max_chars] + _TRUNCATED
    text = _close_fence(text)
    header = f"### {entry.source} · {entry.when} · {entry.who} · {entry.ref}"
    if entry.thread:
        header += f" · thread: {' '.join(scrub_secrets(entry.thread).split())[:_THREAD_CHARS]}"
    return f"{header}\n\n{text}\n"


def _close_fence(text: str) -> str:
    opened: str | None = None
    for line in text.split("\n"):
        found = _FENCE.match(line)
        if found is None:
            continue
        marker = found.group(1)
        if opened is None:
            opened = marker
        elif (
            marker[0] == opened[0]
            and len(marker) >= len(opened)
            and not line[found.end():].strip()
        ):
            opened = None
    return text if opened is None else f"{text}\n{opened}"


def _moment(when: str) -> datetime:
    try:
        moment = datetime.fromisoformat(when)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _private_folder(folder: Path) -> None:
    ensure_real_directory(folder)
    os.chmod(folder, _FOLDER_MODE)


def _write_dated(folder: Path, name: str, text: str, pattern: re.Pattern[str]) -> Path:
    _private_folder(folder)
    path = folder / name
    atomic_write_text(path, text, mode=_FILE_MODE, preserve_existing_mode=False)
    for stale in _dated(folder, pattern)[_KEEP_DIGESTS:]:
        stale.unlink()
    return path


def _dated(folder: Path, pattern: re.Pattern[str]) -> list[Path]:
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return sorted((folder / name for name in names if pattern.fullmatch(name)), reverse=True)
