from __future__ import annotations

import asyncio


def _run(payload: dict):
    from src.brain.actions.handlers.edit_vault_file import execute
    from src.brain.actions.types import validate_vault_note_path
    from src.brain.atomic_io import confined_path, regular_file_sha256
    from src.orchestrator.config import VAULT_PATH

    try:
        target = confined_path(VAULT_PATH, validate_vault_note_path(payload.get("path")))
    except ValueError:
        pass
    else:
        payload.setdefault("expected_sha256", regular_file_sha256(target))
    return asyncio.run(execute(payload))


def test_handler_writes_new_content_when_file_exists(isolated_vault) -> None:
    target = isolated_vault / "Notes" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_text("# old\n\nold body\n", encoding="utf-8")

    result = _run({
        "path": "Notes/x.md",
        "new_content": "# new\n\nnew body\n",
    })
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "# new\n\nnew body\n"


def test_handler_creates_file_when_missing(isolated_vault) -> None:
    target = isolated_vault / "Notes" / "fresh.md"
    result = _run({
        "path": "Notes/fresh.md",
        "new_content": "# fresh\n",
    })
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "# fresh\n"


def test_handler_refuses_path_outside_vault(isolated_vault) -> None:
    result = _run({
        "path": "../escape.md",
        "new_content": "x",
    })
    assert result["ok"] is False
    assert "vault" in result["result"].lower()


def test_handler_accepts_only_visible_markdown_note_paths(isolated_vault) -> None:
    for path in (
        ".obsidian/plugins/agent/main.md",
        "Notes/settings.json",
        "Notes//normalized.md",
        "Notes/../escape.md",
        "Notes/\u202egnp.md",
        "Notes/cafe\u0301.md",
    ):
        result = _run({"path": path, "new_content": "x"})
        assert result["ok"] is False, path
        assert "unsafe vault note path" in result["result"], path


def test_handler_preserves_named_sections(isolated_vault) -> None:
    target = isolated_vault / "daily.md"
    target.write_text(
        "# Day\n\n## Auto Section\n\nold auto\n\n## Quick Capture\n\nuser typed this\n",
        encoding="utf-8",
    )

    result = _run({
        "path": "daily.md",
        "new_content": "# Day\n\n## Auto Section\n\nfresh auto\n",
        "preserve_sections": ["Quick Capture"],
    })
    assert result["ok"] is True
    text = target.read_text(encoding="utf-8")
    assert "fresh auto" in text
    assert "old auto" not in text
    assert "## Quick Capture" in text
    assert "user typed this" in text


def test_handler_no_op_when_content_unchanged(isolated_vault) -> None:
    target = isolated_vault / "same.md"
    target.write_text("# same\n", encoding="utf-8")
    mtime_before = target.stat().st_mtime_ns

    result = _run({
        "path": "same.md",
        "new_content": "# same\n",
    })
    assert result["ok"] is True
    assert "unchanged" in result["result"].lower()
    assert target.stat().st_mtime_ns == mtime_before


def test_handler_refuses_to_overwrite_change_made_after_approval(isolated_vault) -> None:
    from src.brain.atomic_io import regular_file_sha256

    target = isolated_vault / "Note.md"
    target.write_text("approved base\n")
    expected = regular_file_sha256(target)
    target.write_text("newer human edit\n")

    result = _run({
        "path": "Note.md",
        "new_content": "stale agent replacement\n",
        "expected_sha256": expected,
    })

    assert result["ok"] is False and result["retryable"] is True
    assert target.read_text() == "newer human edit\n"
