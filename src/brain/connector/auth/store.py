"""Keep the vault connector's sign-in state in one SQLite file.

The file is ``auth.db`` in the connector's data folder, created with mode
``0600`` whatever the umask is. It has six tables:

- ``clients``: each client that registered, as its full registration JSON. At
  most ``MAX_IDLE_CLIENTS`` registrations without a pending sign-in, code or
  token are kept; beyond that the oldest idle ones are deleted, so strangers
  cannot grow the file, and a client with a live sign-in is never dropped;
- ``pending``: sign-ins waiting for a person's password and code;
- ``codes``: authorization codes waiting to be exchanged for tokens;
- ``tokens``: access and refresh tokens;
- ``settings``: each person's password hash, one-time code secret and last
  one-time code step used, under ``<name>:<person key>``;
- ``mail_reads``: when each sign-in, and each library session, last read mail,
  so the hold on edits after a mail read outlives a token renewal and a restart.

Pending sign-ins, codes and tokens each carry the key of the person they are
for (schema version 2). A file from version 1 gets the ``person`` columns with
``ALTER TABLE`` when it is opened, and ``adopt`` then gives its rows and its
settings to the person with the empty address prefix, so a sign-in from before
keeps working.

Codes and tokens are stored as SHA-256 hashes, never as the strings handed
out, so a copy of the file signs nobody in. The two tokens issued at one
sign-in, and the tokens that later replace them, share a ``sign_in_id``, so
revoking one revokes its partner. Pending sign-ins and codes last 10 minutes,
access tokens 1 hour and refresh tokens 30 days. Expiry times are Unix seconds
from the store's clock: a row is not returned once its time has come, and
expired rows are deleted whenever a row is added. Each call opens its own
connection and runs in one transaction, and every change that must happen
once (using a code, rotating a refresh token) is a single conditional
statement, so two requests racing for the same row cannot both win.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

MAX_IDLE_CLIENTS = 100
HASH_SETTING = "password_hash"
TOTP_SETTING = "totp_secret"
MAIL_HOLD = timedelta(minutes=30)
PENDING_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=10)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
_FILE_MODE = 0o600
_SCHEMA_VERSION = 2
_PERSON_TABLES = ("pending", "codes", "tokens")
_DEFAULT_PERSON = "owner"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS pending (
        pending_id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        params TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        person TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS codes (
        code_hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        params TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        person TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tokens (
        token_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
        client_id TEXT NOT NULL,
        sign_in_id TEXT NOT NULL,
        scopes TEXT NOT NULL,
        resource TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        person TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS tokens_sign_in_id ON tokens (sign_in_id)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS mail_reads (key TEXT PRIMARY KEY, read_at INTEGER NOT NULL)",
)
_LAST_CODE_STEP = "totp_last_step"
_PERSON_SETTINGS = (HASH_SETTING, TOTP_SETTING, _LAST_CODE_STEP)

TokenKind = Literal["access", "refresh"]


@dataclass(frozen=True)
class RequestRow:
    """A pending sign-in or a code: the client, the parameters (JSON) of the request that
    began it, and the key of the person signing in."""

    client_id: str
    params: str
    expires_at: int
    person: str


@dataclass(frozen=True)
class TokenRow:
    client_id: str
    sign_in_id: str
    scopes: tuple[str, ...]
    resource: str
    expires_at: int
    person: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _setting_key(key: str, person: str) -> str:
    return f"{key}:{person}"


def _add_person_columns(conn: sqlite3.Connection) -> None:
    """Give a version 1 file the ``person`` columns; a file that has them is left alone."""
    for table in _PERSON_TABLES:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "person" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN person TEXT NOT NULL DEFAULT ''")


class AuthStore:
    def __init__(self, db_path: Path, clock: Callable[[], datetime] = _utc_now) -> None:
        self.db_path = db_path
        self._clock = clock
        try:
            os.close(os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE))
        except FileExistsError:
            pass
        os.chmod(db_path, _FILE_MODE)
        with self._transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            _add_person_columns(conn)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            yield conn

    def now(self) -> datetime:
        """The current time from the store's clock, in UTC."""
        moment = self._clock()
        if moment.tzinfo is None:
            raise ValueError("the auth store's clock must give times with a time zone")
        return moment.astimezone(UTC)

    def _seconds(self) -> int:
        return int(self.now().timestamp())

    def _purge(self, conn: sqlite3.Connection, now: int) -> None:
        conn.execute("DELETE FROM pending WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM codes WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM tokens WHERE expires_at <= ?", (now,))

    def adopt(self, root_person: str) -> None:
        """Give sign-in state that belongs to nobody to ``root_person``.

        Rows of a version 1 file have the person ``''``; they become
        ``root_person``'s. When ``root_person`` is not the single owner of a
        connector without people, rows and settings of that owner become
        ``root_person``'s too, because they were made at the same addresses.
        The password hash, code secret and last code step are copied to
        ``root_person``'s keys only where those keys are missing. Safe to run at
        every start.
        """
        owners = ("", _DEFAULT_PERSON) if root_person != _DEFAULT_PERSON else ("",)
        marks = ", ".join("?" for _ in owners)
        with self._transaction() as conn:
            for table in _PERSON_TABLES:
                conn.execute(
                    f"UPDATE {table} SET person = ? WHERE person IN ({marks})",  # noqa: S608 - fixed table names and "?" marks only
                    (root_person, *owners),
                )
            for key in _PERSON_SETTINGS:
                sources = [_setting_key(key, _DEFAULT_PERSON)] if len(owners) == 2 else []
                for source in (*sources, key):
                    conn.execute(
                        "INSERT OR IGNORE INTO settings (key, value)"
                        " SELECT ?, value FROM settings WHERE key = ?",
                        (_setting_key(key, root_person), source),
                    )

    def get_setting(self, key: str, person: str) -> str | None:
        """``person``'s value of the setting ``key``, or None when it is not set."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (_setting_key(key, person),)
            ).fetchone()
        return None if row is None else str(row[0])

    def set_setting(self, key: str, value: str, person: str) -> None:
        """Keep ``value`` as ``person``'s setting ``key``."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (_setting_key(key, person), value),
            )

    def claim_code_step(self, step: int, person: str) -> bool:
        """Record ``step`` as ``person``'s last one-time code step used, if it is later than the last one.

        Returns False and changes nothing when ``step`` is not later, so each
        one-time code signs in once.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                " WHERE CAST(settings.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                (_setting_key(_LAST_CODE_STEP, person), str(step)),
            )
        return cursor.rowcount == 1

    def mark_mail_read(self, keys: Sequence[str]) -> None:
        """Record that each of ``keys`` read mail now; rows older than ``MAIL_HOLD`` are deleted."""
        now = self._seconds()
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM mail_reads WHERE read_at <= ?", (now - int(MAIL_HOLD.total_seconds()),)
            )
            for key in keys:
                conn.execute(
                    "INSERT INTO mail_reads (key, read_at) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET read_at = excluded.read_at",
                    (key, now),
                )

    def mail_read_recently(self, keys: Sequence[str], within: timedelta) -> bool:
        """True when any of ``keys`` read mail less than ``within`` ago."""
        if not keys:
            return False
        cutoff = self._seconds() - int(within.total_seconds())
        marks = ", ".join("?" for _ in keys)
        with self._transaction() as conn:
            row = conn.execute(
                f"SELECT 1 FROM mail_reads WHERE key IN ({marks}) AND read_at > ? LIMIT 1",  # noqa: S608 - only "?" marks are joined in
                (*keys, cutoff),
            ).fetchone()
        return row is not None

    def add_client(self, client_id: str, info: str) -> None:
        """Keep a registration, dropping the oldest idle ones beyond ``MAX_IDLE_CLIENTS``."""
        with self._transaction() as conn:
            conn.execute("INSERT INTO clients (client_id, info) VALUES (?, ?)", (client_id, info))
            conn.execute(
                "DELETE FROM clients WHERE rowid IN ("
                " SELECT rowid FROM clients WHERE client_id NOT IN ("
                "  SELECT client_id FROM pending UNION SELECT client_id FROM codes"
                "  UNION SELECT client_id FROM tokens)"
                " ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                (MAX_IDLE_CLIENTS,),
            )

    def get_client(self, client_id: str) -> str | None:
        """The registration JSON of ``client_id``, or None if it never registered."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT info FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def add_pending(self, client_id: str, params: str, person: str) -> str:
        """Keep a sign-in waiting for ``person`` for 10 minutes; return its random id."""
        pending_id = secrets.token_urlsafe(32)
        now = self._seconds()
        with self._transaction() as conn:
            self._purge(conn, now)
            conn.execute(
                "INSERT INTO pending (pending_id, client_id, params, expires_at, person)"
                " VALUES (?, ?, ?, ?, ?)",
                (pending_id, client_id, params, now + int(PENDING_TTL.total_seconds()), person),
            )
        return pending_id

    def get_pending(self, pending_id: str) -> RequestRow | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT client_id, params, expires_at, person FROM pending"
                " WHERE pending_id = ? AND expires_at > ?",
                (pending_id, self._seconds()),
            ).fetchone()
        return None if row is None else RequestRow(*row)

    def pending_to_code(self, pending_id: str, code: str) -> RequestRow | None:
        """Turn a pending sign-in into the authorization code ``code``, once.

        The pending row goes and a code row with the same client, parameters
        and person comes, lasting 10 minutes. Returns the code's row, or None
        when the pending sign-in is unknown, expired or already used.
        """
        now = self._seconds()
        with self._transaction() as conn:
            taken = conn.execute(
                "DELETE FROM pending WHERE pending_id = ? AND expires_at > ?"
                " RETURNING client_id, params, person",
                (pending_id, now),
            ).fetchall()
            if not taken:
                return None
            client_id, params, person = taken[0]
            expires_at = now + int(CODE_TTL.total_seconds())
            self._purge(conn, now)
            conn.execute(
                "INSERT INTO codes (code_hash, client_id, params, expires_at, person)"
                " VALUES (?, ?, ?, ?, ?)",
                (_hash(code), client_id, params, expires_at, person),
            )
        return RequestRow(client_id, params, expires_at, person)

    def get_code(self, code: str, client_id: str) -> RequestRow | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT client_id, params, expires_at, person FROM codes"
                " WHERE code_hash = ? AND client_id = ? AND expires_at > ?",
                (_hash(code), client_id, self._seconds()),
            ).fetchone()
        return None if row is None else RequestRow(*row)

    def take_code(self, code: str, client_id: str) -> RequestRow | None:
        """Use up ``code``: delete it and return its row, or None if it is not there to use."""
        with self._transaction() as conn:
            taken = conn.execute(
                "DELETE FROM codes WHERE code_hash = ? AND client_id = ? AND expires_at > ?"
                " RETURNING client_id, params, expires_at, person",
                (_hash(code), client_id, self._seconds()),
            ).fetchall()
        return RequestRow(*taken[0]) if taken else None

    def _add_token(
        self,
        conn: sqlite3.Connection,
        kind: TokenKind,
        token: str,
        client_id: str,
        sign_in_id: str,
        scopes: Sequence[str],
        resource: str,
        now: int,
        person: str,
    ) -> None:
        lifetime = ACCESS_TTL if kind == "access" else REFRESH_TTL
        conn.execute(
            "INSERT INTO tokens"
            " (token_hash, kind, client_id, sign_in_id, scopes, resource, expires_at, person)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(token),
                kind,
                client_id,
                sign_in_id,
                json.dumps(list(scopes)),
                resource,
                now + int(lifetime.total_seconds()),
                person,
            ),
        )

    def add_tokens(
        self,
        client_id: str,
        scopes: Sequence[str],
        resource: str,
        access: str,
        refresh: str,
        person: str,
    ) -> None:
        """Keep the access and refresh token of ``person``'s new sign-in."""
        sign_in_id = secrets.token_urlsafe(16)
        now = self._seconds()
        with self._transaction() as conn:
            self._purge(conn, now)
            for kind, token in (("access", access), ("refresh", refresh)):
                self._add_token(
                    conn,
                    cast(TokenKind, kind),
                    token,
                    client_id,
                    sign_in_id,
                    scopes,
                    resource,
                    now,
                    person,
                )

    def get_token(self, token: str, kind: TokenKind) -> TokenRow | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT client_id, sign_in_id, scopes, resource, expires_at, person FROM tokens"
                " WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (_hash(token), kind, self._seconds()),
            ).fetchone()
        if row is None:
            return None
        return TokenRow(row[0], row[1], tuple(json.loads(row[2])), row[3], row[4], row[5])

    def rotate_refresh(
        self,
        refresh: str,
        client_id: str,
        access_scopes: Sequence[str],
        new_access: str,
        new_refresh: str,
    ) -> bool:
        """Swap ``refresh`` for a new access and refresh token, in one transaction.

        The old refresh token and every other token of its sign-in go. The new
        refresh token keeps the old one's scopes, resource and person; the new access
        token gets ``access_scopes``. Returns False and changes nothing when
        ``refresh`` is unknown, expired or belongs to another client.
        """
        now = self._seconds()
        with self._transaction() as conn:
            taken = conn.execute(
                "DELETE FROM tokens WHERE token_hash = ? AND kind = 'refresh' AND client_id = ?"
                " AND expires_at > ? RETURNING sign_in_id, scopes, resource, person",
                (_hash(refresh), client_id, now),
            ).fetchall()
            if not taken:
                return False
            sign_in_id, scopes, resource, person = taken[0]
            conn.execute("DELETE FROM tokens WHERE sign_in_id = ?", (sign_in_id,))
            self._purge(conn, now)
            self._add_token(
                conn,
                "access",
                new_access,
                client_id,
                sign_in_id,
                access_scopes,
                resource,
                now,
                person,
            )
            self._add_token(
                conn,
                "refresh",
                new_refresh,
                client_id,
                sign_in_id,
                json.loads(scopes),
                resource,
                now,
                person,
            )
        return True

    def revoke(self, token: str) -> None:
        """Delete ``token`` and every other token of its sign-in; an unknown token changes nothing."""
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM tokens WHERE sign_in_id IN"
                " (SELECT sign_in_id FROM tokens WHERE token_hash = ?)",
                (_hash(token),),
            )

    def revoke_all(self, person: str | None = None) -> int:
        """Delete every token, code and pending sign-in of ``person``, or of everyone when None.

        Returns how many tokens and codes went. Registered clients stay, so
        claude.ai can sign in again without registering anew.
        """
        where, args = ("", ()) if person is None else (" WHERE person = ?", (person,))
        with self._transaction() as conn:
            tokens = conn.execute(f"DELETE FROM tokens{where}", args).rowcount  # noqa: S608 - fixed text only
            codes = conn.execute(f"DELETE FROM codes{where}", args).rowcount  # noqa: S608 - fixed text only
            conn.execute(f"DELETE FROM pending{where}", args)  # noqa: S608 - fixed text only
        return tokens + codes
