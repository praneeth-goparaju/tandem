from src.brain import state
from src.db import get_db


def test_set_then_get_roundtrip(isolated_db):
    state.set_state("email_watermark:gmail:INBOX", "12345")
    row = get_db().execute(
        "SELECT value FROM daemon_state WHERE key=?", ("email_watermark:gmail:INBOX",)
    ).fetchone()
    assert row[0] == "12345"


def test_set_overwrites(isolated_db):
    state.set_state("k_overwrite", "1")
    state.set_state("k_overwrite", "2")
    row = get_db().execute(
        "SELECT value FROM daemon_state WHERE key=?", ("k_overwrite",)
    ).fetchone()
    assert row[0] == "2"
