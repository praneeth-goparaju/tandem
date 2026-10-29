from datetime import date

from src.brain.runtime import edit_backups


def test_day_folders_older_than_thirty_days_are_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    root = tmp_path / "vault-edit-backups"
    for name in ("2026-09-01", "2026-09-17", "2026-10-17", "notes"):
        (root / name).mkdir(parents=True)
    assert edit_backups.delete_old(30, today=date(2026, 10, 17)) == 1
    assert sorted(p.name for p in root.iterdir()) == ["2026-09-17", "2026-10-17", "notes"]


def test_a_new_file_has_nothing_to_keep(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    assert edit_backups.keep_copy(tmp_path / "missing.md", "missing.md") is None


def test_every_new_folder_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    note = tmp_path / "n.md"
    note.write_text("x")
    copy = edit_backups.keep_copy(note, "Health/Deep/n.md")
    for folder in (copy.parent, copy.parent.parent, copy.parent.parent.parent, tmp_path / "vault-edit-backups"):
        assert folder.stat().st_mode & 0o777 == 0o700, folder
