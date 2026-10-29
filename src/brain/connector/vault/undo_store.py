"""Keep what each vault edit replaced, and the edits waiting for the owner.

The store is one SQLite file in the connector's data folder, created with
mode ``0600`` whatever the umask is, because it holds note text. It has two
tables. ``edits`` has one row per edit that ran: the passage it replaced
(``old``), the text put in its place (``new``), where in the note that text
starts (``start``, a character offset) and the note's hash right after the
edit, which ``undo`` checks before putting ``old`` back. ``held`` has one row
per edit that the rules held for the Approve button, with the hash the note
had when the chat read it.

Ids are random URL-safe strings, never counters, so one id says nothing about
another. Times are stored in UTC as ISO text. Every call opens its own
connection and runs in one transaction. Rows older than 90 days are removed by
``purge``.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

KEEP_FOR = timedelta(days=90)
_FILE_MODE = 0o600
_SCHEMA_VERSION = 1
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS edits (
        edit_id TEXT PRIMARY KEY,
        rel TEXT NOT NULL,
        old TEXT NOT NULL,
        new TEXT NOT NULL,
        hash_after TEXT NOT NULL,
        start INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS edits_created_at ON edits (created_at)",
    """
    CREATE TABLE IF NOT EXISTS held (
        held_id TEXT PRIMARY KEY,
        rel TEXT NOT NULL,
        old TEXT NOT NULL,
        new TEXT NOT NULL,
        expected_hash TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS held_created_at ON held (created_at)",
)


@dataclass
class UndoRow:
    edit_id: str
    rel: str
    old: str
    new: str
    hash_after: str
    created_at: datetime
    start: int


@dataclass
class HeldRow:
    held_id: str
    rel: str
    old: str
    new: str
    expected_hash: str
    reason: str
    created_at: datetime


def _stamp(when: datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("times given to the undo store must carry a time zone")
    return when.astimezone(UTC).isoformat(timespec="microseconds")


def _now() -> str:
    return _stamp(datetime.now(UTC))


def _new_id() -> str:
    return secrets.token_urlsafe(16)


class UndoStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        try:
            os.close(os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE))
        except FileExistsError:
            pass
        os.chmod(db_path, _FILE_MODE)
        with self._transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            yield conn

    def record(self, rel: str, old: str, new: str, hash_after: str, start: int) -> str:
        """Record an edit that ran and return its id."""
        edit_id = _new_id()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO edits (edit_id, rel, old, new, hash_after, start, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (edit_id, rel, old, new, hash_after, start, _now()),
            )
        return edit_id

    def get(self, edit_id: str) -> UndoRow:
        """Return the edit with ``edit_id``; raises ``KeyError`` if there is none."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT edit_id, rel, old, new, hash_after, created_at, start"
                " FROM edits WHERE edit_id = ?",
                (edit_id,),
            ).fetchone()
        if row is None:
            raise KeyError(edit_id)
        return UndoRow(
            edit_id=row[0],
            rel=row[1],
            old=row[2],
            new=row[3],
            hash_after=row[4],
            created_at=datetime.fromisoformat(row[5]),
            start=row[6],
        )

    def hold(self, rel: str, old: str, new: str, expected_hash: str, reason: str) -> str:
        """Keep an edit for the Approve button and return its held id."""
        held_id = _new_id()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO held (held_id, rel, old, new, expected_hash, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (held_id, rel, old, new, expected_hash, reason, _now()),
            )
        return held_id

    def take_held(self, held_id: str) -> HeldRow:
        """Return the held edit; raises ``KeyError`` if there is none.

        The row stays in the store until ``drop_held`` removes it, so an edit
        that cannot run yet can still be shown in the error.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT held_id, rel, old, new, expected_hash, reason, created_at"
                " FROM held WHERE held_id = ?",
                (held_id,),
            ).fetchone()
        if row is None:
            raise KeyError(held_id)
        return HeldRow(
            held_id=row[0],
            rel=row[1],
            old=row[2],
            new=row[3],
            expected_hash=row[4],
            reason=row[5],
            created_at=datetime.fromisoformat(row[6]),
        )

    def drop_held(self, held_id: str) -> None:
        """Remove a held edit once it has run."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM held WHERE held_id = ?", (held_id,))

    def edits_since(self, since: datetime) -> int:
        """Count the edits that ran at or after ``since``."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM edits WHERE created_at >= ?", (_stamp(since),)
            ).fetchone()
        return int(row[0])

    def purge(self, now: datetime) -> int:
        """Delete edit and held rows older than 90 days; return how many went."""
        cutoff = _stamp(now - KEEP_FOR)
        with self._transaction() as conn:
            edits = conn.execute("DELETE FROM edits WHERE created_at < ?", (cutoff,)).rowcount
            held = conn.execute("DELETE FROM held WHERE created_at < ?", (cutoff,)).rowcount
        return edits + held
