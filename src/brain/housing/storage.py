from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from src.brain.sqlite_security import secure_sqlite_files, secure_sqlite_path
from src.orchestrator.config import MEMORY_DB_PATH

from .models import Listing

DB_PATH = Path(MEMORY_DB_PATH).parent / "housing.db"


def get_db() -> sqlite3.Connection:
    path = secure_sqlite_path(DB_PATH)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    os.chmod(path, 0o600, follow_symlinks=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_schema(conn)
    secure_sqlite_files(path)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id            TEXT PRIMARY KEY,      -- "<source>:<source_id>"
            source        TEXT NOT NULL,
            source_id     TEXT NOT NULL,
            url           TEXT NOT NULL,
            title         TEXT NOT NULL,
            price_eur     INTEGER,
            rooms         REAL,
            sqm           REAL,
            address       TEXT,
            postcode      TEXT,
            city          TEXT,
            lat           REAL,
            lon           REAL,
            raw_json      TEXT,
            first_seen    TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen     TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price_eur);
        CREATE INDEX IF NOT EXISTS idx_listings_city  ON listings(city);

        CREATE TABLE IF NOT EXISTS watches (
            name          TEXT PRIMARY KEY,
            criteria_json TEXT NOT NULL,
            last_run_at   TEXT,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watch_hits (
            watch_name    TEXT NOT NULL,
            listing_id    TEXT NOT NULL,
            first_hit_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (watch_name, listing_id)
        );
    """)
    conn.commit()


def upsert_listings(conn: sqlite3.Connection, listings: Iterable[Listing]) -> list[Listing]:
    new = []
    for lst in listings:
        existing = conn.execute(
            "SELECT 1 FROM listings WHERE id=?", (lst.id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE listings SET last_seen=CURRENT_TIMESTAMP, price_eur=?, title=? WHERE id=?",
                (lst.price_eur, lst.title, lst.id),
            )
        else:
            conn.execute(
                """INSERT INTO listings
                   (id, source, source_id, url, title, price_eur, rooms, sqm,
                    address, postcode, city, lat, lon, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lst.id, lst.source, lst.source_id, lst.url, lst.title,
                    lst.price_eur, lst.rooms, lst.sqm, lst.address, lst.postcode,
                    lst.city, lst.lat, lst.lon, json.dumps(lst.raw, ensure_ascii=False),
                ),
            )
            new.append(lst)
    conn.commit()
    return new


def save_watch(conn: sqlite3.Connection, name: str, criteria: dict) -> None:
    conn.execute(
        """INSERT INTO watches(name, criteria_json) VALUES(?,?)
           ON CONFLICT(name) DO UPDATE SET criteria_json=excluded.criteria_json""",
        (name, json.dumps(criteria)),
    )
    conn.commit()


def delete_watch(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM watches WHERE name=?", (name,))
    conn.execute("DELETE FROM watch_hits WHERE watch_name=?", (name,))
    conn.commit()
    return cur.rowcount > 0


def list_watches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT name, criteria_json, last_run_at FROM watches ORDER BY name"
    ).fetchall()
    return [
        {"name": r[0], "criteria": json.loads(r[1]), "last_run_at": r[2]}
        for r in rows
    ]


def record_watch_hits(
    conn: sqlite3.Connection, watch_name: str, listing_ids: Iterable[str]
) -> list[str]:
    new_hits = []
    for lid in listing_ids:
        existing = conn.execute(
            "SELECT 1 FROM watch_hits WHERE watch_name=? AND listing_id=?",
            (watch_name, lid),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO watch_hits(watch_name, listing_id) VALUES(?,?)",
                (watch_name, lid),
            )
            new_hits.append(lid)
    conn.execute(
        "UPDATE watches SET last_run_at=CURRENT_TIMESTAMP WHERE name=?", (watch_name,)
    )
    conn.commit()
    return new_hits
