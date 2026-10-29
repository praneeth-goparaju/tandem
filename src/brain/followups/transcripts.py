from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any

from src.brain.followups.entries import Entry

_SDK_ENTRYPOINTS = ("sdk-cli", "sdk-py")
_SYNTHETIC_FLAGS = ("isMeta", "isCompactSummary", "isVisibleInTranscriptOnly")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_NO_FOLLOW = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class _Lines:
    since: datetime
    until: datetime | None
    claude_dir: str
    excluded_paths: frozenset[str]


def iter_transcript_entries(
    projects_dir: Path,
    *,
    since: datetime,
    until: datetime | None = None,
    home: Path,
    exclude_projects: frozenset[str],
    exclude_paths: frozenset[str] = frozenset(),
) -> Iterator[Entry]:
    """Yield the owner's typed text and Claude's replies from recent transcripts.

    Claude Code names a project's folder after its path with every character but an ASCII
    letter or digit as `-`, so the folder of a path inside a project (a subfolder, or a
    worktree under it) starts with the project's folder name and `-`.
    """
    lines = _Lines(
        since=since,
        until=until,
        claude_dir=str(home / ".claude"),
        excluded_paths=frozenset(name.casefold() for name in exclude_paths),
    )
    excluded = tuple(name.casefold() for name in exclude_projects)
    for folder, name in _recent_files(projects_dir, since.timestamp(), excluded):
        yield from _file_entries(folder, name, lines)


def _excluded(folder: str, names: tuple[str, ...]) -> bool:
    folded = folder.casefold()
    return any(folded == name or folded.startswith(name + "-") for name in names)


def _recent_files(
    projects_dir: Path, cutoff: float, excluded: tuple[str, ...]
) -> list[tuple[Path, str]]:
    found: list[tuple[float, Path, str]] = []
    for folder in _listing(projects_dir):
        if not folder.is_dir(follow_symlinks=False) or _excluded(folder.name, excluded):
            continue
        for file in _listing(Path(folder.path)):
            if not file.name.endswith(".jsonl") or not file.is_file(follow_symlinks=False):
                continue
            try:
                modified = file.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if modified >= cutoff:
                found.append((modified, Path(folder.path), file.name))
    found.sort()
    return [(folder, name) for _, folder, name in found]


def _listing(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError:
        return []


def _open_transcript(folder: Path, name: str) -> IO[str]:
    folder_fd = os.open(folder, _NO_FOLLOW | getattr(os, "O_DIRECTORY", 0))
    try:
        fd = os.open(name, _NO_FOLLOW, dir_fd=folder_fd)
    finally:
        os.close(folder_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {name}")
        return os.fdopen(fd, encoding="utf-8", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def _file_entries(folder: Path, name: str, lines: _Lines) -> Iterator[Entry]:
    project = folder.name
    try:
        with _open_transcript(folder, name) as handle:
            for line_no, raw in enumerate(handle, start=1):
                entry = _entry(raw, f"{project}/{name}:{line_no}", project, lines)
                if entry is not None:
                    yield entry
    except OSError:
        return


def _entry(raw: str, ref: str, folder: str, lines: _Lines) -> Entry | None:
    try:
        line = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(line, dict):
        return None
    kind = line.get("type")
    if kind == "user":
        who = "owner"
    elif kind == "assistant":
        who = "claude"
    else:
        return None
    if line.get("isSidechain") or line.get("entrypoint") in _SDK_ENTRYPOINTS:
        return None
    if kind == "user" and any(line.get(flag) for flag in _SYNTHETIC_FLAGS):
        return None
    cwd = line.get("cwd")
    if _is_under(cwd, lines.claude_dir) or _has_component(cwd, lines.excluded_paths):
        return None
    moment = _parse_time(line.get("timestamp"))
    if (
        moment is None
        or moment < lines.since
        or (lines.until is not None and moment >= lines.until)
    ):
        return None
    message = line.get("message")
    text = _text(message.get("content") if isinstance(message, dict) else None)
    if kind == "user":
        text = _REMINDER.sub("", text).strip()
        if text.startswith("<"):
            return None
    if not text:
        return None
    return Entry(
        source="claude_code",
        ref=ref,
        who=who,
        when=line["timestamp"],
        text=text,
        place="private",
        project=folder,
    )


def _is_under(cwd: Any, directory: str) -> bool:
    return isinstance(cwd, str) and (cwd == directory or cwd.startswith(directory + os.sep))


def _has_component(cwd: Any, names: frozenset[str]) -> bool:
    if not names or not isinstance(cwd, str):
        return False
    return any(part.casefold() in names for part in PurePosixPath(cwd).parts)


def _parse_time(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts).strip()
