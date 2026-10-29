from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from src.brain.atomic_io import (
    StaleWriteError,
    atomic_create_text,
    atomic_write_text,
    atomic_write_text_if_sha256,
    commit_staged,
    confined_path,
    read_regular_text,
    regular_file_sha256,
    stage_text,
)


def test_atomic_create_is_no_clobber_and_read_is_no_follow(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    atomic_create_text(target, "first")
    assert read_regular_text(target) == "first"

    with pytest.raises(FileExistsError):
        atomic_create_text(target, "second")
    assert target.read_text() == "first"

    outside = tmp_path / "outside"
    outside.write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(OSError):
        read_regular_text(link)


def test_atomic_create_conflict_stages_private_even_if_target_is_public(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "note.md"
    target.write_text("existing")
    target.chmod(0o644)
    real_link = os.link

    def inspect_link(src, dst, **kwargs):
        info = os.stat(src, dir_fd=kwargs["src_dir_fd"], follow_symlinks=False)
        assert stat.S_IMODE(info.st_mode) == 0o600
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", inspect_link)
    with pytest.raises(FileExistsError):
        atomic_create_text(target, "sensitive replacement")

    assert target.read_text() == "existing"
    assert not list(tmp_path.glob(".*.tmp"))


def test_directory_fsync_io_error_is_not_silenced(tmp_path: Path, monkeypatch) -> None:
    real_fsync = os.fsync

    def fail_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "disk I/O error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError) as exc_info:
        atomic_write_text(tmp_path / "state.txt", "body")
    assert exc_info.value.errno == errno.EIO


def test_atomic_write_is_private_for_new_files_and_preserves_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "state.txt"
    atomic_write_text(target, "first")
    assert target.read_text() == "first"
    assert target.stat().st_mode & 0o777 == 0o600

    target.chmod(0o640)
    atomic_write_text(target, "second")
    assert target.read_text() == "second"
    assert target.stat().st_mode & 0o777 == 0o640


def test_conditional_write_refuses_stale_or_unexpected_destination(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    assert regular_file_sha256(target) is None
    atomic_write_text_if_sha256(target, "first", None)
    approved = regular_file_sha256(target)
    assert approved is not None

    target.write_text("human edit")
    with pytest.raises(StaleWriteError):
        atomic_write_text_if_sha256(target, "agent edit", approved)
    assert target.read_text() == "human edit"

    with pytest.raises(StaleWriteError):
        atomic_write_text_if_sha256(target, "unexpected replace", None)


def test_atomic_write_rejects_symlink_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("safe")
    link = tmp_path / "link"
    link.symlink_to(outside)

    with pytest.raises(OSError, match="non-regular"):
        atomic_write_text(link, "unsafe")
    assert outside.read_text() == "safe"


def test_atomic_write_rejects_symlink_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="path component"):
        atomic_write_text(linked_parent / "note.md", "unsafe")
    assert not (outside / "note.md").exists()


def test_staged_file_must_share_destination_directory(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    staged = stage_text(left / "note.md", "body")
    try:
        with pytest.raises(OSError, match="destination directory"):
            commit_staged(staged, right / "note.md")
    finally:
        os.unlink(staged)


def test_commit_rejects_parent_swapped_to_symlink(tmp_path: Path) -> None:
    parent = tmp_path / "vault"
    parent.mkdir()
    staged = stage_text(parent / "note.md", "body")
    parked = tmp_path / "parked"
    parent.rename(parked)
    outside = tmp_path / "outside"
    outside.mkdir()
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="path component"):
        commit_staged(staged, parent / "note.md")
    assert not (outside / "note.md").exists()
    assert (parked / staged.name).read_text() == "body"


def test_confined_path_rejects_traversal_absolute_paths_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    assert confined_path(root, "notes/new.md") == root / "notes" / "new.md"
    for value in ("../outside/file", str(outside / "file"), "linked/file"):
        with pytest.raises(ValueError):
            confined_path(root, value)
