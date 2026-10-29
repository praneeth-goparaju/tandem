from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.orchestrator import config


def test_only_the_repo_root_env_file_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("BRAIN_TEST_FROM_PARENT=leaked\n", encoding="utf-8")
    repo = tmp_path / "worktrees" / "repo"
    repo.mkdir(parents=True)
    monkeypatch.delenv("BRAIN_TEST_FROM_PARENT", raising=False)
    monkeypatch.chdir(repo)

    assert config.load_repo_env(repo) is False
    assert "BRAIN_TEST_FROM_PARENT" not in os.environ


def test_the_repo_root_env_file_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("BRAIN_TEST_FROM_REPO=yes\n", encoding="utf-8")
    monkeypatch.delenv("BRAIN_TEST_FROM_REPO", raising=False)

    assert config.load_repo_env(tmp_path) is True
    assert os.environ["BRAIN_TEST_FROM_REPO"] == "yes"
    monkeypatch.delenv("BRAIN_TEST_FROM_REPO")


def test_the_code_reads_the_env_file_of_its_own_checkout() -> None:
    assert config.REPO_ROOT == Path(__file__).resolve().parents[1]
