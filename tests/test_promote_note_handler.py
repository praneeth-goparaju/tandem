from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path


def _notes_dir():
    from src.orchestrator import config
    return config.AGENT_NOTES_DIR


def test_promote_writes_vault_note_and_logs_event(isolated_db, isolated_vault):
    from src.brain.actions.handlers.promote_note import execute
    from src.db import utcnow

    body = "NAS SSH uses key ~/.ssh/nas_ed25519, not the default.\nPort is 2222."
    result = asyncio.run(execute({
        "title": "NAS SSH access",
        "body": body,
        "area": "infra",
        "agent": "discovery",
        "_action_id": "act123",
    }))
    assert result["ok"] is True

    notes = sorted(_notes_dir().glob("*.md"))
    assert len(notes) == 1
    note = notes[0]
    assert note.name == f"{utcnow()[:10]}-nas-ssh-access-act123.md"

    text = note.read_text(encoding="utf-8")
    assert text.startswith(body)
    footer = text.rstrip("\n").splitlines()[-1]
    assert footer == f"Promoted by discovery on {utcnow()[:10]}, area infra, action act123"
    assert [p for p in _notes_dir().iterdir() if p.suffix != ".md"] == []

    conn = isolated_db.get_db()
    actor, action, subject, trust, detail = conn.execute(
        "SELECT actor, action, subject, trust, detail FROM events WHERE action='note_promoted'"
    ).fetchone()
    assert actor == "discovery"
    assert subject == "NAS SSH access"
    assert trust == "trusted"
    assert detail == f"vault:Agent Notes/{note.name}"


def test_promote_footer_omits_optional_fields(isolated_db, isolated_vault):
    from src.brain.actions.handlers.promote_note import execute
    from src.db import utcnow

    result = asyncio.run(execute({"title": "plain note", "body": "just a fact", "agent": "career"}))
    assert result["ok"] is True
    note = next(_notes_dir().glob("*-plain-note-*.md"))
    footer = note.read_text(encoding="utf-8").rstrip("\n").splitlines()[-1]
    assert footer == f"Promoted by career on {utcnow()[:10]}"


def test_promote_defaults_title_from_body(isolated_db, isolated_vault):
    from src.brain.actions.handlers.promote_note import execute

    result = asyncio.run(execute({
        "body": "buy trimmer blades\nmodel ER-GP82",
        "agent": "health",
    }))
    assert result["ok"] is True

    notes = list(_notes_dir().glob("*-buy-trimmer-blades-*.md"))
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert text.startswith("buy trimmer blades\nmodel ER-GP82")


def test_promote_missing_body(isolated_db):
    from src.brain.actions.handlers.promote_note import execute

    result = asyncio.run(execute({"title": "x", "agent": "a"}))
    assert result["ok"] is False
    assert "body" in result["result"]

    result = asyncio.run(execute({"body": "   ", "agent": "a"}))
    assert result["ok"] is False
    assert "body" in result["result"]


def test_promote_same_title_twice_gets_stable_distinct_filenames(isolated_db, isolated_vault):
    from src.brain.actions.handlers.promote_note import execute
    from src.db import utcnow

    assert asyncio.run(execute({"title": "same topic", "body": "first note", "agent": "a"}))["ok"] is True
    assert asyncio.run(execute({"title": "same topic", "body": "second note", "agent": "a"}))["ok"] is True

    names = sorted(p.name for p in _notes_dir().glob("*.md"))
    assert len(names) == 2
    assert all(name.startswith(f"{utcnow()[:10]}-same-topic-") for name in names)
    assert len(set(names)) == 2
    first_lines = sorted(p.read_text(encoding="utf-8").splitlines()[0] for p in _notes_dir().glob("*.md"))
    assert first_lines == ["first note", "second note"]


def test_failed_publish_leaves_no_note_and_no_event(isolated_db, isolated_vault, monkeypatch):
    from src.brain.actions.handlers.promote_note import execute

    def failing_link(src, dst, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "link", failing_link)
    result = asyncio.run(execute({"title": "doomed", "body": "doomed body", "agent": "a"}))
    assert result["ok"] is False
    assert result["retryable"] is True
    assert "vault note write failed" in result["result"]

    assert list(_notes_dir().iterdir()) == []

    conn = isolated_db.get_db()
    assert conn.execute(
        "SELECT count(*) FROM events WHERE action='note_promoted'"
    ).fetchone()[0] == 0


def test_same_action_retry_is_idempotent_and_logs_once(isolated_db, isolated_vault):
    from src.brain.actions.handlers.promote_note import execute

    payload = {
        "title": "stable",
        "body": "one body",
        "agent": "a",
        "_action_id": "abc123",
        "_action_created_at": "2026-08-01T10:00:00+00:00",
    }
    first = asyncio.run(execute(payload))
    second = asyncio.run(execute(payload))

    assert first["ok"] is True and second["ok"] is True
    assert "already promoted" in second["result"]
    assert [p.name for p in _notes_dir().glob("*.md")] == [
        "2026-08-01-stable-abc123.md"
    ]
    count = isolated_db.get_db().execute(
        "SELECT count(*) FROM events WHERE action='note_promoted'"
    ).fetchone()[0]
    assert count == 1


def test_post_publish_audit_failure_is_terminal_partial_success(
    isolated_db, isolated_vault, monkeypatch
):
    from src.brain.actions.handlers import promote_note

    def fail_audit(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(promote_note, "log_event_once", fail_audit)
    payload = {
        "title": "recoverable",
        "body": "stable body",
        "agent": "a",
        "_action_id": "action-recovery",
        "_action_created_at": "2026-08-01T10:00:00+00:00",
    }
    result = asyncio.run(promote_note.execute(payload))
    assert result["ok"] is True
    assert result["partial"] is True
    assert "manual audit reconciliation required" in result["result"]
    assert len(list(_notes_dir().glob("*.md"))) == 1
    assert isolated_db.get_db().execute(
        "SELECT count(*) FROM events WHERE action='note_promoted'"
    ).fetchone()[0] == 0


def test_promote_rejects_notes_directory_outside_vault(
    isolated_db, isolated_vault, monkeypatch, tmp_path: Path
):
    from src.brain.actions.handlers.promote_note import execute
    from src.orchestrator import config

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    monkeypatch.setattr(config, "AGENT_NOTES_DIR", outside)
    result = asyncio.run(execute({"title": "escape", "body": "secret", "agent": "a"}))

    assert result["ok"] is False
    assert not result.get("retryable", False)
    assert "inside the configured vault" in result["result"]
    assert not outside.exists()


def test_promote_rejects_invalid_executor_date(isolated_db, isolated_vault):
    from src.brain.actions.handlers.promote_note import execute

    result = asyncio.run(execute({
        "title": "escape",
        "body": "secret",
        "agent": "a",
        "_action_id": "abc123",
        "_action_created_at": "../../outside",
    }))

    assert result["ok"] is False
    assert not _notes_dir().exists()
