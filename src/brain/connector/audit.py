"""Record every tool call of the vault connector, one JSON line per call.

The log is a folder with one file per month, named ``audit-YYYY-MM.jsonl`` from
the UTC time of the call. Each line has the keys ``time`` (UTC, ISO 8601),
``tool``, ``target``, ``result``, ``token_id`` and ``args``, written with sorted
keys. The folder is created with mode ``0700`` and the files with mode
``0600``, because the lines name notes. Result and argument text longer than
200 characters is cut to 200 characters followed by ``...``.

A line is added with one ``write`` call, so lines from different calls never mix.
If the line cannot be written (disk full, no permission), ``write`` records a
warning in the server log and returns, so a tool call is never failed by its
own log. A wrong argument type still raises. ``prune`` removes the files of
months older than 12 months and leaves every other file alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

_logger = logging.getLogger(__name__)

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_MAX_TEXT = 200
_KEEP_MONTHS = 12
_FILE_NAME = re.compile(r"audit-(\d{4})-(0[1-9]|1[0-2])\.jsonl")


def _cut(text: str) -> str:
    if len(text) <= _MAX_TEXT:
        return text
    return text[:_MAX_TEXT] + "..."


def _utc(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class AuditLog:
    def __init__(self, dir: Path) -> None:
        self._dir = dir
        dir.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)

    def write(
        self,
        tool: str,
        target: str,
        result: str,
        token_id: str,
        args_summary: str,
        now: datetime | None = None,
    ) -> None:
        moment = _utc(now)
        line = (
            json.dumps(
                {
                    "time": moment.isoformat(),
                    "tool": tool,
                    "target": target,
                    "result": _cut(result),
                    "token_id": token_id,
                    "args": _cut(args_summary),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        path = self._dir / f"audit-{moment:%Y-%m}.jsonl"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as error:
            _logger.warning("audit log line for %s not written to %s: %s", tool, path, error)

    def prune(self, now: datetime) -> int:
        current = _utc(now)
        oldest_kept = current.year * 12 + current.month - _KEEP_MONTHS
        removed = 0
        for path in self._dir.iterdir():
            match = _FILE_NAME.fullmatch(path.name)
            if match is None or not path.is_file():
                continue
            if int(match.group(1)) * 12 + int(match.group(2)) < oldest_kept:
                path.unlink()
                removed += 1
        return removed
