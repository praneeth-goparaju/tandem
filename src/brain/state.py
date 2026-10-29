from __future__ import annotations

from src.db import get_db


def set_state(key: str, value: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO daemon_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
