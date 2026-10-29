"""Tests for scripts/vault_snapshot.sh, the nightly vault snapshot."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vault_snapshot.sh"


def run_snapshot(vault: Path, git_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(vault), str(git_dir)],
        capture_output=True,
        text=True,
    )


def git(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_snapshot_commits_and_restores(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    git_dir = tmp_path / "snap.git"
    note = vault / "a.md"
    note.write_text("one")
    (vault / ".trash").mkdir()
    (vault / ".trash" / "x.md").write_text("deleted")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.json").write_text("{}")
    (vault / ".obsidian" / "workspace-mobile.json").write_text("{}")
    (vault / ".obsidian" / "app.json").write_text("{}")

    first = run_snapshot(vault, git_dir)
    assert first.returncode == 0, first.stderr
    first_commit = first.stdout.strip()
    assert first_commit == git(git_dir, "rev-parse", "HEAD").strip()

    note.write_text("two")
    second = run_snapshot(vault, git_dir)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == git(git_dir, "rev-parse", "HEAD").strip()
    assert second.stdout.strip() != first_commit

    assert git(git_dir, "show", "HEAD~1:a.md") == "one"
    assert git(git_dir, "show", "HEAD:a.md") == "two"

    third = run_snapshot(vault, git_dir)
    assert third.returncode == 0, third.stderr
    assert third.stdout.strip() == "no changes"

    tracked = git(git_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "a.md" in tracked
    assert ".obsidian/app.json" in tracked
    assert ".trash/x.md" not in tracked
    assert ".obsidian/workspace.json" not in tracked
    assert ".obsidian/workspace-mobile.json" not in tracked


def test_fresh_git_dir_gets_exclusions(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("one")
    git_dir = tmp_path / "snap.git"

    result = run_snapshot(vault, git_dir)
    assert result.returncode == 0, result.stderr

    lines = (git_dir / "info" / "exclude").read_text().splitlines()
    assert ".obsidian/workspace*.json" in lines
    assert ".trash/" in lines


def test_existing_exclude_is_kept(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("one")
    (vault / ".DS_Store").write_text("junk")
    git_dir = tmp_path / "snap.git"
    subprocess.run(["git", "init", "--bare", "-q", str(git_dir)], check=True)
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".DS_Store\n.trash/\n")

    result = run_snapshot(vault, git_dir)
    assert result.returncode == 0, result.stderr

    lines = exclude.read_text().splitlines()
    assert lines.count(".DS_Store") == 1
    assert lines.count(".trash/") == 1
    assert ".obsidian/workspace*.json" in lines
    tracked = git(git_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".DS_Store" not in tracked


def test_missing_vault_path_fails(tmp_path):
    vault = tmp_path / "missing-vault"
    git_dir = tmp_path / "snap.git"

    result = run_snapshot(vault, git_dir)
    assert result.returncode != 0


def test_git_status_failure_is_not_reported_as_no_changes(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "a.md"
    note.write_text("one")
    git_dir = tmp_path / "snap.git"
    assert run_snapshot(vault, git_dir).returncode == 0

    # Break the repository: point the branch at an object that does not exist.
    branch = git(git_dir, "symbolic-ref", "HEAD").strip()
    (git_dir / branch).write_text("1" * 40 + "\n")
    note.write_text("two")

    result = run_snapshot(vault, git_dir)
    assert result.returncode != 0
    assert "no changes" not in result.stdout


def test_existing_exclude_without_final_newline(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("one")
    git_dir = tmp_path / "snap.git"
    subprocess.run(["git", "init", "--bare", "-q", str(git_dir)], check=True)
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".DS_Store")

    result = run_snapshot(vault, git_dir)
    assert result.returncode == 0, result.stderr

    lines = exclude.read_text().splitlines()
    assert ".DS_Store" in lines
    assert ".trash/" in lines
    assert ".obsidian/workspace*.json" in lines
