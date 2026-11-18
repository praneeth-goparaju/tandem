"""Keep what each vault edit replaced, and the edits waiting for the owner.

The store is one SQLite file in the connector's data folder, created with
mode ``0600`` whatever the umask is, because it holds note text. It has two
tables. ``edits`` has one row per edit that ran: the passage it replaced
(``old``), the text put in its place (``new``), where in the note that text
starts (``start``, a character offset) and the note's hash right after the
edit, which ``undo`` checks before putting ``old`` back. ``held`` has one row
per edit that the rules held for the Approve button, with the hash the note
had when the chat read it.

Both tables carry the key of the person whose chat made the edit (schema
version 2). The edit limit is counted per person, and a held edit can be
confirmed only by its person. A file from version 1 gets the ``person``
column with ``ALTER TABLE`` when it is opened; ``adopt`` gives its rows, which
have the person ``''``, to the person with the empty address prefix.

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
_SCHEMA_VERSION = 2
_PERSON_TABLES = ("edits", "held")
_DEFAULT_PERSON = "owner"
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS edits (
        edit_id TEXT PRIMARY KEY,
        rel TEXT NOT NULL,
        old TEXT NOT NULL,
        new TEXT NOT NULL,
        hash_after TEXT NOT NULL,
        start INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        person TEXT NOT NULL DEFAULT ''
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
        created_at TEXT NOT NULL,
        person TEXT NOT NULL DEFAULT ''
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
    person: str = ""


@dataclass
class HeldRow:
    held_id: str
    rel: str
    old: str
    new: str
    expected_hash: str
    reason: str
    created_at: datetime
    person: str = ""


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
            for table in _PERSON_TABLES:
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "person" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN person TEXT NOT NULL DEFAULT ''")
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            yield conn

    def adopt(self, root_person: str) -> None:
        """Give edits and held edits that belong to nobody to ``root_person``.

        Rows of a version 1 file have the person ``''``. When ``root_person`` is
        not the single owner of a connector without people, that owner's rows
        become ``root_person``'s too. Safe to run at every start.
        """
        owners = ("", _DEFAULT_PERSON) if root_person != _DEFAULT_PERSON else ("",)
        marks = ", ".join("?" for _ in owners)
        with self._transaction() as conn:
            for table in _PERSON_TABLES:
                conn.execute(
                    f"UPDATE {table} SET person = ? WHERE person IN ({marks})",  # noqa: S608 - fixed table names and "?" marks only
                    (root_person, *owners),
                )

    def record(
        self,
        rel: str,
        old: str,
        new: str,
        hash_after: str,
        start: int,
        person: str = _DEFAULT_PERSON,
    ) -> str:
        """Record an edit that ``person``'s chat ran and return its id."""
        edit_id = _new_id()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO edits (edit_id, rel, old, new, hash_after, start, created_at, person)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (edit_id, rel, old, new, hash_after, start, _now(), person),
            )
        return edit_id

    def get(self, edit_id: str) -> UndoRow:
        """Return the edit with ``edit_id``; raises ``KeyError`` if there is none."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT edit_id, rel, old, new, hash_after, created_at, start, person"
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
            person=row[7],
        )

    def hold(
        self,
        rel: str,
        old: str,
        new: str,
        expected_hash: str,
        reason: str,
        person: str = _DEFAULT_PERSON,
    ) -> str:
        """Keep ``person``'s edit for their Approve button and return its held id."""
        held_id = _new_id()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO held"
                " (held_id, rel, old, new, expected_hash, reason, created_at, person)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (held_id, rel, old, new, expected_hash, reason, _now(), person),
            )
        return held_id

    def take_held(self, held_id: str) -> HeldRow:
        """Return the held edit; raises ``KeyError`` if there is none.

        The row stays in the store until ``drop_held`` removes it, so an edit
        that cannot run yet can still be shown in the error.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT held_id, rel, old, new, expected_hash, reason, created_at, person"
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
            person=row[7],
        )

    def drop_held(self, held_id: str) -> None:
        """Remove a held edit once it has run."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM held WHERE held_id = ?", (held_id,))

    def edits_since(self, since: datetime, person: str = _DEFAULT_PERSON) -> int:
        """Count the edits of ``person``'s chats that ran at or after ``since``."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM edits WHERE created_at >= ? AND person = ?",
                (_stamp(since), person),
            ).fetchone()
        return int(row[0])

    def purge(self, now: datetime) -> int:
        """Delete edit and held rows older than 90 days; return how many went."""
        cutoff = _stamp(now - KEEP_FOR)
        with self._transaction() as conn:
            edits = conn.execute("DELETE FROM edits WHERE created_at < ?", (cutoff,)).rowcount
            held = conn.execute("DELETE FROM held WHERE created_at < ?", (cutoff,)).rowcount
        return edits + held
