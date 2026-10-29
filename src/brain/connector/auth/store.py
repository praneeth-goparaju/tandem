"""Keep the vault connector's sign-in state in one SQLite file.

The file is ``auth.db`` in the connector's data folder, created with mode
``0600`` whatever the umask is. It has five tables:

- ``clients``: each client that registered, as its full registration JSON. At
  most ``MAX_IDLE_CLIENTS`` registrations without a pending sign-in, code or
  token are kept; beyond that the oldest idle ones are deleted, so strangers
  cannot grow the file, and a client with a live sign-in is never dropped;
- ``pending``: sign-ins waiting for the owner's password and code;
- ``codes``: authorization codes waiting to be exchanged for tokens;
- ``tokens``: access and refresh tokens;
- ``settings``: the password hash, the one-time code secret and the last
  one-time code step used.

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
from typing import Literal

MAX_IDLE_CLIENTS = 100
HASH_SETTING = "password_hash"
TOTP_SETTING = "totp_secret"
PENDING_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=10)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
_FILE_MODE = 0o600
_SCHEMA_VERSION = 1
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS pending (
        pending_id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        params TEXT NOT NULL,
        expires_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS codes (
        code_hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        params TEXT NOT NULL,
        expires_at INTEGER NOT NULL
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
        expires_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS tokens_sign_in_id ON tokens (sign_in_id)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_LAST_CODE_STEP = "totp_last_step"

TokenKind = Literal["access", "refresh"]


@dataclass(frozen=True)
class RequestRow:
    """A pending sign-in or a code: the client and the parameters (JSON) of the request that began it."""

    client_id: str
    params: str
    expires_at: int


@dataclass(frozen=True)
class TokenRow:
    client_id: str
    scopes: tuple[str, ...]
    resource: str
    expires_at: int


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


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

    def get_setting(self, key: str) -> str | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def claim_code_step(self, step: int) -> bool:
        """Record ``step`` as the last one-time code step used, if it is later than the last one.

        Returns False and changes nothing when ``step`` is not later, so each
        one-time code signs in once.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                " WHERE CAST(settings.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                (_LAST_CODE_STEP, str(step)),
            )
        return cursor.rowcount == 1

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

    def add_pending(self, client_id: str, params: str) -> str:
        """Keep a sign-in waiting for the owner for 10 minutes; return its random id."""
        pending_id = secrets.token_urlsafe(32)
        now = self._seconds()
        with self._transaction() as conn:
            self._purge(conn, now)
            conn.execute(
                "INSERT INTO pending (pending_id, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                (pending_id, client_id, params, now + int(PENDING_TTL.total_seconds())),
            )
        return pending_id

    def get_pending(self, pending_id: str) -> RequestRow | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT client_id, params, expires_at FROM pending WHERE pending_id = ? AND expires_at > ?",
                (pending_id, self._seconds()),
            ).fetchone()
        return None if row is None else RequestRow(row[0], row[1], row[2])

    def pending_to_code(self, pending_id: str, code: str) -> RequestRow | None:
        """Turn a pending sign-in into the authorization code ``code``, once.

        The pending row goes and a code row with the same client and
        parameters comes, lasting 10 minutes. Returns the code's row, or None
        when the pending sign-in is unknown, expired or already used.
        """
        now = self._seconds()
        with self._transaction() as conn:
            taken = conn.execute(
                "DELETE FROM pending WHERE pending_id = ? AND expires_at > ? RETURNING client_id, params",
                (pending_id, now),
            ).fetchall()
            if not taken:
                return None
            client_id, params = taken[0]
            expires_at = now + int(CODE_TTL.total_seconds())
            self._purge(conn, now)
            conn.execute(
                "INSERT INTO codes (code_hash, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                (_hash(code), client_id, params, expires_at),
            )
        return RequestRow(client_id, params, expires_at)

    def get_code(self, code: str, client_id: str) -> RequestRow | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT client_id, params, expires_at FROM codes"
                " WHERE code_hash = ? AND client_id = ? AND expires_at > ?",
                (_hash(code), client_id, self._seconds()),
            ).fetchone()
        return None if row is None else RequestRow(row[0], row[1], row[2])

    def take_code(self, code: str, client_id: str) -> RequestRow | None:
        """Use up ``code``: delete it and return its row, or None if it is not there to use."""
        with self._transaction() as conn:
            taken = conn.execute(
                "DELETE FROM codes WHERE code_hash = ? AND client_id = ? AND expires_at > ?"
                " RETURNING client_id, params, expires_at",
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
    ) -> None:
        lifetime = ACCESS_TTL if kind == "access" else REFRESH_TTL
        conn.execute(
            "INSERT INTO tokens (token_hash, kind, client_id, sign_in_id, scopes, resource, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(token),
                kind,
                client_id,
                sign_in_id,
                json.dumps(list(scopes)),
                resource,
                now + int(lifetime.total_seconds()),
            ),
        )

    def add_tokens(
        self, client_id: str, scopes: Sequence[str], resource: str, access: str, refresh: str
    ) -> None:
        """Keep the access and refresh token of a new sign-in."""
        sign_in_id = secrets.token_urlsafe(16)
        now = self._seconds()
        with self._transaction() as conn:
            self._purge(conn, now)
            self._add_token(conn, "access", access, client_id, sign_in_id, scopes, resource, now)
            self._add_token(conn, "refresh", refresh, client_id, sign_in_id, scopes, resource, now)

    def get_token(self, token: str, kind: TokenKind) -> TokenRow | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT client_id, scopes, resource, expires_at FROM tokens"
                " WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (_hash(token), kind, self._seconds()),
            ).fetchone()
        if row is None:
            return None
        return TokenRow(row[0], tuple(json.loads(row[1])), row[2], row[3])

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
        refresh token keeps the old one's scopes and resource; the new access
        token gets ``access_scopes``. Returns False and changes nothing when
        ``refresh`` is unknown, expired or belongs to another client.
        """
        now = self._seconds()
        with self._transaction() as conn:
            taken = conn.execute(
                "DELETE FROM tokens WHERE token_hash = ? AND kind = 'refresh' AND client_id = ?"
                " AND expires_at > ? RETURNING sign_in_id, scopes, resource",
                (_hash(refresh), client_id, now),
            ).fetchall()
            if not taken:
                return False
            sign_in_id, scopes, resource = taken[0]
            conn.execute("DELETE FROM tokens WHERE sign_in_id = ?", (sign_in_id,))
            self._purge(conn, now)
            self._add_token(
                conn, "access", new_access, client_id, sign_in_id, access_scopes, resource, now
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

    def revoke_all(self) -> int:
        """Delete every token, code and pending sign-in; return how many tokens and codes went.

        Registered clients stay, so claude.ai can sign in again without
        registering anew.
        """
        with self._transaction() as conn:
            tokens = conn.execute("DELETE FROM tokens").rowcount
            codes = conn.execute("DELETE FROM codes").rowcount
            conn.execute("DELETE FROM pending")
        return tokens + codes
