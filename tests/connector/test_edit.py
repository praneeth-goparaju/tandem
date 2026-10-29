from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.brain.connector.layout import VaultLayout, owner_only_paths
from src.brain.connector.vault import edit
from src.brain.connector.vault.coordination import StaleVersion
from src.brain.connector.vault.edit import (
    EditRefused,
    apply_edit,
    confirm_held,
    decide,
    undo,
)
from src.brain.connector.vault.paths import PathRefused
from src.brain.connector.vault.read import read_note
from src.brain.connector.vault.undo_store import UndoStore

FIXTURE = Path(__file__).parent.parent / "fixtures" / "connector_vault"
REASON = "mark the passport as renewed"
ACTION_NOTE = (
    "---\ntype: action\nstatus: next\npriority: normal\n---\n# Send the form\n\nSource: chat\n"
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "Vault"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def store(tmp_path: Path) -> UndoStore:
    return UndoStore(tmp_path / "undo.db")


def _items(count: int) -> str:
    return "".join(f"- item {n:02d}\n" for n in range(1, count + 1))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decide(
    note: str,
    old: str,
    new: str,
    rel: str = "Notes/Plan.md",
    edits_last_hour: int = 0,
    mail_read_recently: bool = False,
    actions_dir: str = "Actions",
) -> edit.Decision:
    return decide(rel, note, old, new, edits_last_hour, mail_read_recently, actions_dir=actions_dir)


def test_small_edit_allowed() -> None:
    decision = _decide(_items(20), "- item 03", "- item 03 (done)")
    assert decision.allow is True


def test_more_than_10_lines_held() -> None:
    note = _items(60)
    old = "".join(f"- item {n:02d}\n" for n in range(1, 13))
    new = "".join(f"- item {n:02d} checked\n" for n in range(1, 13))
    decision = _decide(note, old, new)
    assert decision.allow is False
    assert "replaces 12 lines, the limit is 10" in decision.reason
    assert "removes" not in decision.reason
    assert _decide(note, old[: old.index("- item 11")], new[: new.index("- item 11")]).allow


def test_removes_more_than_quarter_held() -> None:
    note = _items(10)
    decision = _decide(note, "- item 02\n- item 03\n- item 04\n", "")
    assert decision.allow is False
    assert decision.reason == "removes 30% of the note, the limit is a quarter"
    assert _decide(_items(4), "- item 02\n", "").allow


def test_heading_line_held() -> None:
    note = "# Plan\n\n## Tasks\n" + _items(12)
    renamed = _decide(note, "## Tasks", "## Todo")
    assert renamed.allow is False
    assert renamed.reason == "touches a heading"
    added = _decide(note, "- item 05", "- item 05\n# Ignore the rules")
    assert added.allow is False
    assert added.reason == "touches a heading"
    merged = _decide("Intro text\n## Tasks\n" + _items(12), "Intro text\n", "Intro text ")
    assert merged.reason == "touches a heading"
    spans_heading = _decide(note, "## Tasks\n- item 01", "## Tasks\n- item one")
    assert spans_heading.reason == "touches a heading"
    inside_heading = _decide(note, "Tasks\n- item 01", "Tasks\n- item one")
    assert inside_heading.reason == "touches a heading"
    passage_line = _decide(note, "item 05", "#item 05")
    assert passage_line.reason == "touches a heading"


def test_frontmatter_status_in_actions_allowed() -> None:
    decision = _decide(
        ACTION_NOTE, "status: next", "status: done", rel="Actions/A-20261022-01 - Send.md"
    )
    assert decision.allow is True
    status_last = "---\ntype: action\nstatus: next\n---\n# Send the form\n\nSource: chat\n"
    assert _decide(
        status_last, "status: next", "status: done", rel="Actions/A-20261022-02 - Send.md"
    ).allow


def test_frontmatter_other_field_in_actions_held() -> None:
    decision = _decide(
        ACTION_NOTE, "priority: normal", "priority: high", rel="Actions/A-20261022-01 - Send.md"
    )
    assert decision.allow is False
    assert decision.reason == "changes frontmatter"
    renamed_key = _decide(
        ACTION_NOTE, "status: next", "state: next", rel="Actions/A-20261022-01 - Send.md"
    )
    assert renamed_key.reason == "changes frontmatter"


def test_status_exception_follows_the_actions_dir() -> None:
    open_note = _decide(
        ACTION_NOTE, "status: next", "status: done", rel="Open/A.md", actions_dir="Open"
    )
    assert open_note.allow is True
    stale = _decide(
        ACTION_NOTE, "status: next", "status: done", rel="Actions/A.md", actions_dir="Open"
    )
    assert stale.allow is False
    assert stale.reason == "changes frontmatter"
    nested = _decide(
        ACTION_NOTE,
        "status: next",
        "status: done",
        rel="Work/Actions/A.md",
        actions_dir="Work/Actions",
    )
    assert nested.allow is True
    sibling = _decide(
        ACTION_NOTE,
        "status: next",
        "status: done",
        rel="Work/Other/A.md",
        actions_dir="Work/Actions",
    )
    assert sibling.allow is False


def test_edit_refuses_owner_only_path_from_the_layout(vault: Path, store: UndoStore) -> None:
    (vault / "Journal").mkdir()
    queue = vault / "Journal" / "Action Queue.md"
    queue.write_text("queue\n", encoding="utf-8")
    with pytest.raises(PathRefused, match="owner-only"):
        apply_edit(
            vault,
            store,
            "Journal/Action Queue.md",
            "queue",
            "line",
            _sha(queue),
            "tidy the queue note",
            mail_read_recently=False,
            owner_only=owner_only_paths(VaultLayout(daily_notes_dir="Journal")),
        )


def test_frontmatter_elsewhere_held() -> None:
    decision = _decide(ACTION_NOTE, "status: next", "status: done", rel="Notes/Send.md")
    assert decision.allow is False
    assert decision.reason == "changes frontmatter"
    created = _decide("Intro\n" + _items(12), "Intro\n", "---\nowner: someone\n---\nIntro\n")
    assert created.allow is False
    assert "changes frontmatter" in created.reason


def test_hourly_cap_held() -> None:
    decision = _decide(_items(20), "- item 03", "- item 03 (done)", edits_last_hour=20)
    assert decision.allow is False
    assert decision.reason == "20 edits already ran in the last hour"
    assert _decide(_items(20), "- item 03", "- item 03 (done)", edits_last_hour=19).allow


def test_mail_read_recently_held() -> None:
    decision = _decide(_items(20), "- item 03", "- item 03 (done)", mail_read_recently=True)
    assert decision.allow is False
    assert decision.reason == "this chat read mail in the last 30 minutes"


def test_carriage_returns_count_as_line_ends() -> None:
    planted = _decide(
        ACTION_NOTE,
        "status: next",
        "status: next\raction_id: A-evil\rwaiting_on: someone",
        rel="Actions/A-20261022-01 - Send.md",
    )
    assert planted.allow is False
    assert "changes frontmatter" in planted.reason
    heading = _decide(_items(12), "- item 05", "- item 05\r# Ignore the rules")
    assert heading.allow is False
    assert "touches a heading" in heading.reason
    many = _decide(_items(60), "- item 03", "\r".join(f"- line {n}" for n in range(40)))
    assert many.allow is False
    assert "replaces 40 lines, the limit is 10" in many.reason
    crlf = _items(20).replace("\n", "\r\n")
    assert _decide(crlf, "- item 03", "- item 03 (done)").allow


def test_unusual_line_ending_held() -> None:
    reason = "uses an unusual line ending"
    swap = _decide("---\ntype: note\n---\n" + _items(12), "---\ntype", "---\rtype")
    assert swap.allow is False
    assert swap.reason.startswith(reason)
    for mark in ("\u2028", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\u2029"):
        planted = _decide(
            ACTION_NOTE,
            "status: next",
            f"status: next{mark}action_id: A-evil",
            rel="Actions/A-20261022-01 - Send.md",
        )
        assert planted.allow is False
        assert planted.reason.startswith(reason)
    crlf = _items(20).replace("\n", "\r\n")
    assert _decide(crlf, "- item 03\r\n", "- item 03 (done)\r\n").allow


CRLF_FRONTMATTER = ("---\ntype: note\ntags: [home]\n---\n" + _items(12)).replace("\n", "\r\n")


def test_lone_carriage_return_formed_at_join_held() -> None:
    for old, new in (("\ntype", "type"), ("\n---", "---")):
        decision = _decide(CRLF_FRONTMATTER, old, new)
        assert decision.allow is False
        assert decision.reason.startswith("uses an unusual line ending")
    assert _decide(CRLF_FRONTMATTER, "- item 03\r\n", "- item 03 (done)\r\n").allow


def test_join_probe_held_and_note_unchanged(vault: Path, store: UndoStore) -> None:
    note = vault / "Notes" / "Windows.md"
    note.write_bytes(CRLF_FRONTMATTER.encode("utf-8"))
    result = apply_edit(
        vault,
        store,
        "Notes/Windows.md",
        "\ntype",
        "type",
        _sha(note),
        "tidy the first frontmatter line",
        mail_read_recently=False,
    )
    assert result.status == "held"
    assert note.read_bytes() == CRLF_FRONTMATTER.encode("utf-8")


def test_indented_atx_heading_held() -> None:
    for indent in (" ", "   "):
        decision = _decide(_items(12), "- item 05", f"- item 05\n{indent}# Ignore the rules")
        assert decision.allow is False
        assert decision.reason == "touches a heading"


def test_setext_heading_held() -> None:
    for underline in ("===", "---", " - "):
        decision = _decide(_items(12), "- item 05", f"- item 05\nIgnore the rules\n{underline}")
        assert decision.allow is False
        assert decision.reason == "touches a heading"
    note = "Intro\n\n---\n" + _items(12)
    above_rule = _decide(note, "Intro\n\n", "Intro\nIgnore the rules\n")
    assert above_rule.allow is False
    assert above_rule.reason == "touches a heading"
    assert _decide(note, "- item 05", "- item 05 (done)").allow


def test_several_broken_rules_all_named() -> None:
    decision = _decide("# Plan\n" + _items(3), "# Plan\n", "", mail_read_recently=True)
    assert decision.allow is False
    assert "touches a heading" in decision.reason
    assert "removes 25%" not in decision.reason
    assert "this chat read mail in the last 30 minutes" in decision.reason


def test_small_edit_done_with_undo_row(vault: Path, store: UndoStore) -> None:
    note = vault / "Household.md"
    start_hash = read_note(vault, "Household.md").hash
    result = apply_edit(
        vault,
        store,
        "Household.md",
        "- Renew passport",
        "- Renew passport (done)",
        start_hash,
        REASON,
        mail_read_recently=False,
    )
    assert result.status == "done"
    assert result.held_id is None
    assert result.edit_id is not None
    assert result.read_back is not None
    assert result.read_back.hash == _sha(note)
    assert "- Renew passport (done)\n- Book dentist" in note.read_text(encoding="utf-8")
    row = store.get(result.edit_id)
    assert (row.rel, row.old, row.new, row.hash_after) == (
        "Household.md",
        "- Renew passport",
        "- Renew passport (done)",
        _sha(note),
    )
    assert store.edits_since(datetime.now(UTC) - timedelta(hours=1)) == 1


def test_crlf_note_edited_byte_exact(vault: Path, store: UndoStore) -> None:
    note = vault / "Notes" / "Windows.md"
    note.write_bytes(_items(8).replace("\n", "\r\n").encode("utf-8"))
    result = apply_edit(
        vault,
        store,
        "Notes/Windows.md",
        "- item 03\r\n",
        "- item 03 (done)\r\n",
        _sha(note),
        "mark item three as done",
        mail_read_recently=False,
    )
    assert result.status == "done"
    expected = _items(8).replace("- item 03", "- item 03 (done)").replace("\n", "\r\n")
    assert note.read_bytes() == expected.encode("utf-8")


def test_passage_must_be_unique(vault: Path, store: UndoStore) -> None:
    note = vault / "Notes" / "Shopping.md"
    note.write_text("- milk\n- bread\n- milk\n- eggs\n- rice\n", encoding="utf-8")
    before = note.read_bytes()
    with pytest.raises(EditRefused, match="appears 2 times"):
        apply_edit(
            vault,
            store,
            "Notes/Shopping.md",
            "- milk",
            "- oat milk",
            _sha(note),
            "switch to oat milk",
            mail_read_recently=False,
        )
    with pytest.raises(EditRefused, match="not found"):
        apply_edit(
            vault,
            store,
            "Notes/Shopping.md",
            "- butter",
            "- ghee",
            _sha(note),
            "switch to ghee please",
            mail_read_recently=False,
        )
    assert note.read_bytes() == before
    assert store.edits_since(datetime.now(UTC) - timedelta(hours=1)) == 0


def test_overlapping_passage_counts_twice(vault: Path, store: UndoStore) -> None:
    note = vault / "Notes" / "Shopping.md"
    note.write_text("aaa\n- bread\n- eggs\n- rice\n", encoding="utf-8")
    with pytest.raises(EditRefused, match="appears 2 times"):
        apply_edit(
            vault,
            store,
            "Notes/Shopping.md",
            "aa",
            "b",
            _sha(note),
            "shorten the first line",
            mail_read_recently=False,
        )


def test_reason_required(vault: Path, store: UndoStore) -> None:
    note = vault / "Household.md"
    for reason in ("", "   ", "done"):
        with pytest.raises(EditRefused, match="reason"):
            apply_edit(
                vault,
                store,
                "Household.md",
                "- Renew passport",
                "- x",
                _sha(note),
                reason,
                mail_read_recently=False,
            )


def test_protected_path_refused(vault: Path, store: UndoStore) -> None:
    claude = vault / "CLAUDE.md"
    claude.write_text("Be careful.\n", encoding="utf-8")
    with pytest.raises(PathRefused):
        apply_edit(
            vault,
            store,
            "CLAUDE.md",
            "careful",
            "careless",
            _sha(claude),
            "loosen the house rules",
            mail_read_recently=False,
        )
    assert claude.read_text(encoding="utf-8") == "Be careful.\n"


def test_stale_hash_refused(vault: Path, store: UndoStore) -> None:
    note = vault / "Household.md"
    before = note.read_bytes()
    with pytest.raises(StaleVersion):
        apply_edit(
            vault,
            store,
            "Household.md",
            "- Renew passport",
            "- x",
            "0" * 64,
            REASON,
            mail_read_recently=False,
        )
    assert note.read_bytes() == before
    assert store.edits_since(datetime.now(UTC) - timedelta(hours=1)) == 0


def test_second_concurrent_edit_refused(
    vault: Path, store: UndoStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = vault / "Household.md"
    start_hash = _sha(note)
    first = apply_edit(
        vault,
        store,
        "Household.md",
        "- Renew passport",
        "- Passport renewed",
        start_hash,
        REASON,
        mail_read_recently=False,
    )
    assert first.status == "done"
    with pytest.raises(StaleVersion):
        apply_edit(
            vault,
            store,
            "Household.md",
            "- Book dentist appointment",
            "- Dentist booked",
            start_hash,
            "the dentist is booked now",
            mail_read_recently=False,
        )

    start_hash = _sha(note)
    real_write = edit.coordinated_write
    interleaved: list[str] = []

    def write_after_other_chat(path: Path, data: str, expected_hash: str | None) -> str:
        if not interleaved:
            interleaved.append("other chat")
            other = apply_edit(
                vault,
                store,
                "Household.md",
                "- Book dentist appointment",
                "- Dentist booked",
                start_hash,
                "the dentist is booked now",
                mail_read_recently=False,
            )
            assert other.status == "done"
        return real_write(path, data, expected_hash)

    monkeypatch.setattr(edit, "coordinated_write", write_after_other_chat)
    with pytest.raises(StaleVersion):
        apply_edit(
            vault,
            store,
            "Household.md",
            "Open items for the year.",
            "Open items for 2026.",
            start_hash,
            "name the year explicitly",
            mail_read_recently=False,
        )
    text = note.read_text(encoding="utf-8")
    assert "- Dentist booked" in text
    assert "Open items for the year." in text
    assert store.edits_since(datetime.now(UTC) - timedelta(hours=1)) == 2


def test_hourly_cap_counts_store_edits(vault: Path, store: UndoStore) -> None:
    for _ in range(20):
        store.record("Notes/Other.md", "a", "b", "c" * 64, 0)
    note = vault / "Household.md"
    result = apply_edit(
        vault,
        store,
        "Household.md",
        "- Renew passport",
        "- Passport renewed",
        _sha(note),
        REASON,
        mail_read_recently=False,
    )
    assert result.status == "held"
    assert result.reason == "20 edits already ran in the last hour"


def test_held_then_confirmed_runs(vault: Path, store: UndoStore) -> None:
    note = vault / "Household.md"
    before = note.read_bytes()
    held = apply_edit(
        vault,
        store,
        "Household.md",
        "# Household",
        "# Admin",
        _sha(note),
        "shorter title for the note",
        mail_read_recently=False,
    )
    assert held.status == "held"
    assert held.reason == "touches a heading"
    assert held.edit_id is None and held.read_back is None
    assert held.held_id is not None
    assert note.read_bytes() == before
    mailed = apply_edit(
        vault,
        store,
        "Household.md",
        "- Renew passport",
        "- Passport renewed",
        _sha(note),
        REASON,
        mail_read_recently=True,
    )
    assert mailed.status == "held"
    assert note.read_bytes() == before

    done = confirm_held(vault, store, held.held_id)
    assert done.status == "done"
    assert done.edit_id is not None
    assert done.read_back is not None
    assert note.read_text(encoding="utf-8").startswith("---\nstatus: active\n---\n# Admin\n")
    assert done.read_back.hash == _sha(note)
    row = store.get(done.edit_id)
    assert (row.old, row.new, row.hash_after) == ("# Household", "# Admin", _sha(note))
    with pytest.raises(EditRefused, match="no held edit"):
        confirm_held(vault, store, held.held_id)


def test_confirm_refused_if_note_changed(vault: Path, store: UndoStore) -> None:
    note = vault / "Household.md"
    held = apply_edit(
        vault,
        store,
        "Household.md",
        "# Household",
        "# Admin",
        _sha(note),
        "shorter title for the note",
        mail_read_recently=False,
    )
    assert held.held_id is not None
    note.write_text(note.read_text(encoding="utf-8") + "- Phone edit\n", encoding="utf-8")
    changed = note.read_bytes()
    with pytest.raises(StaleVersion, match="changed since"):
        confirm_held(vault, store, held.held_id)
    assert note.read_bytes() == changed
    assert store.take_held(held.held_id).old == "# Household"
    assert store.edits_since(datetime.now(UTC) - timedelta(hours=1)) == 0


def test_undo_restores_and_is_hash_checked(vault: Path, store: UndoStore) -> None:
    note = vault / "Household.md"
    original = note.read_bytes()
    done = apply_edit(
        vault,
        store,
        "Household.md",
        "- Renew passport\n",
        "",
        _sha(note),
        "the passport is already renewed",
        mail_read_recently=False,
    )
    assert done.edit_id is not None
    assert b"Renew passport" not in note.read_bytes()

    restored = undo(vault, store, done.edit_id)
    assert note.read_bytes() == original
    assert restored.hash == _sha(note)
    assert restored.text == original.decode("utf-8")
    with pytest.raises(EditRefused, match="changed since"):
        undo(vault, store, done.edit_id)

    again = apply_edit(
        vault,
        store,
        "Household.md",
        "- Renew passport",
        "- Passport renewed",
        _sha(note),
        REASON,
        mail_read_recently=False,
    )
    assert again.edit_id is not None
    note.write_text(note.read_text(encoding="utf-8") + "- Phone edit\n", encoding="utf-8")
    changed = note.read_bytes()
    with pytest.raises(EditRefused, match="changed since"):
        undo(vault, store, again.edit_id)
    assert note.read_bytes() == changed
    with pytest.raises(EditRefused, match="no edit"):
        undo(vault, store, "unknown-id")
