from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "make_public_tree.sh"
LEAK = "/Users/" + "jsmith" + "/x"
REAL_ADDRESS = "t.owner@" + "mailhost.io"


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = Test Owner\n\temail = owner@example.com\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(config)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["HOME"] = str(tmp_path)
    return env


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    )
    return result.stdout


@pytest.fixture
def source_repo(tmp_path: Path, git_env: dict[str, str]) -> Path:
    """A copy of the script and the scanner in a tmp git repo with two files."""
    repo = tmp_path / "source"
    (repo / "tools").mkdir(parents=True)
    (repo / "src" / "brain").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "tools" / "make_public_tree.sh")
    shutil.copy(REPO_ROOT / "tools" / "check_pii.py", repo / "tools" / "check_pii.py")
    shutil.copy(
        REPO_ROOT / "tools" / "personal_terms_public.txt",
        repo / "tools" / "personal_terms_public.txt",
    )
    shutil.copy(
        REPO_ROOT / "src" / "brain" / "atomic_io.py", repo / "src" / "brain" / "atomic_io.py"
    )
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "brain" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "user").mkdir()
    (repo / ".gitignore").write_text("user/\ntools/pii_blocklist.txt\n", encoding="utf-8")
    blocklist = repo / "tools" / "pii_blocklist.txt"
    blocklist.write_text("# header\nzzqx-blocklist-term\n", encoding="utf-8")
    blocklist.chmod(0o600)
    terms = repo / "user" / "private_terms.txt"
    terms.write_text("zzqx-private-term\n\n# note\nzzqx-second-term\n", encoding="utf-8")
    terms.chmod(0o600)
    (repo / "clean.md").write_text("nothing personal here\n", encoding="utf-8")
    (repo / "bad.md").write_text(f"see {LEAK}\n", encoding="utf-8")
    _git(repo, git_env, "init", "-q", "-b", "main")
    _git(repo, git_env, "add", "-A")
    _git(repo, git_env, "commit", "-q", "-m", "source")
    return repo


def _run(repo: Path, env: dict[str, str], out_dir: Path | None) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(repo / "tools" / "make_public_tree.sh")]
    if out_dir is not None:
        args.append(str(out_dir))
    return subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True)


def test_script_refuses_a_missing_argument(source_repo: Path, git_env: dict[str, str]) -> None:
    result = _run(source_repo, git_env, None)

    assert result.returncode == 2
    assert "usage" in result.stderr


def test_script_refuses_non_empty_out_dir(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine\n", encoding="utf-8")

    result = _run(source_repo, git_env, out)

    assert result.returncode == 2
    assert "not empty" in result.stderr
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_script_stops_on_a_finding(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 1
    assert "bad.md:1: absolute-home-path" in result.stderr
    assert (out / "clean.md").is_file()
    assert not (out / ".git").exists()
    assert "jsmith" not in result.stdout + result.stderr


def test_script_builds_single_commit_tree(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _git(source_repo, git_env, "rm", "-q", "bad.md")
    _git(source_repo, git_env, "commit", "-q", "-m", "drop the bad file")
    source_head = _git(source_repo, git_env, "rev-parse", "HEAD")
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 0, result.stderr
    log = _git(out, git_env, "log", "--format=%an|%ae|%s").splitlines()
    assert log == ["Test Owner|owner@example.com|Initial public release"]
    commit = _git(out, git_env, "rev-parse", "--short", "HEAD").strip()
    assert commit in result.stdout
    assert "push" in result.stdout
    assert "Private terms applied: 3" in result.stdout
    assert "Source: main at " in result.stdout
    assert "zzqx" not in result.stdout + result.stderr
    assert _git(out, git_env, "branch", "--show-current").strip() == "main"
    assert not (out / "user").exists()
    assert (out / "clean.md").is_file()
    assert _git(source_repo, git_env, "rev-parse", "HEAD") == source_head
    assert _git(source_repo, git_env, "status", "--porcelain") == ""


def test_script_works_through_a_symlinked_parent_folder(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _git(source_repo, git_env, "rm", "-q", "bad.md")
    _git(source_repo, git_env, "commit", "-q", "-m", "drop the bad file")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    result = _run(source_repo, git_env, link / "out")

    assert result.returncode == 0, result.stderr
    assert len(_git(real / "out", git_env, "log", "--oneline").splitlines()) == 1


@pytest.mark.parametrize("missing", ["tools/pii_blocklist.txt", "user/private_terms.txt"])
def test_script_refuses_without_the_private_terms(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path, missing: str
) -> None:
    (source_repo / missing).unlink()
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 2
    assert missing in result.stderr
    assert not out.exists()


@pytest.mark.parametrize("empty", ["tools/pii_blocklist.txt", "user/private_terms.txt"])
def test_script_refuses_a_terms_file_with_no_terms(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path, empty: str
) -> None:
    (source_repo / empty).write_text("# only a comment\n\n", encoding="utf-8")
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 2
    assert empty in result.stderr
    assert not out.exists()


def test_script_refuses_an_empty_argument(source_repo: Path, git_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(source_repo / "tools" / "make_public_tree.sh"), ""],
        cwd=source_repo,
        env=git_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "usage" in result.stderr


def test_script_refuses_an_out_dir_inside_the_source_repo(
    source_repo: Path, git_env: dict[str, str]
) -> None:
    result = _run(source_repo, git_env, source_repo / "build" / "out")

    assert result.returncode == 2
    assert "inside" in result.stderr
    assert not (source_repo / "build").exists()


def test_script_ignores_git_environment_variables(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _git(source_repo, git_env, "rm", "-q", "bad.md")
    _git(source_repo, git_env, "commit", "-q", "-m", "drop the bad file")
    head = _git(source_repo, git_env, "rev-parse", "HEAD")
    index = _git(source_repo, git_env, "ls-files", "-s")
    leaky_env = dict(git_env, GIT_DIR=str(source_repo / ".git"))
    out = tmp_path / "out"

    result = _run(source_repo, leaky_env, out)

    assert result.returncode == 0, result.stderr
    assert _git(source_repo, git_env, "rev-parse", "HEAD") == head
    assert _git(source_repo, git_env, "ls-files", "-s") == index
    assert _git(source_repo, git_env, "status", "--porcelain") == ""
    assert len(_git(out, git_env, "log", "--oneline").splitlines()) == 1


def test_script_ignores_global_hooks_and_excludes(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _git(source_repo, git_env, "rm", "-q", "bad.md")
    _git(source_repo, git_env, "commit", "-q", "-m", "drop the bad file")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    excludes = tmp_path / "excludes"
    excludes.write_text("clean.md\n", encoding="utf-8")
    config = Path(git_env["GIT_CONFIG_GLOBAL"])
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"[core]\n\thooksPath = {hooks}\n\texcludesFile = {excludes}\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 0, result.stderr
    assert (
        _git(out, git_env, "rev-parse", "HEAD^{tree}")
        == _git(source_repo, git_env, "rev-parse", "HEAD^{tree}")
    )


def test_script_fails_when_the_built_tree_differs_from_head(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _git(source_repo, git_env, "rm", "-q", "bad.md")
    (source_repo / ".gitattributes").write_text("clean.md export-ignore\n", encoding="utf-8")
    _git(source_repo, git_env, "add", "-A")
    _git(source_repo, git_env, "commit", "-q", "-m", "mark a file export-ignore")

    result = _run(source_repo, git_env, tmp_path / "out")

    assert result.returncode == 1
    assert "differs" in result.stderr


def test_script_warns_when_the_source_tree_is_dirty(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _git(source_repo, git_env, "rm", "-q", "bad.md")
    _git(source_repo, git_env, "commit", "-q", "-m", "drop the bad file")
    (source_repo / "clean.md").write_text("changed but not committed\n", encoding="utf-8")

    result = _run(source_repo, git_env, tmp_path / "out")

    assert result.returncode == 0, result.stderr
    assert "uncommitted" in result.stderr
    assert (tmp_path / "out" / "clean.md").read_text(encoding="utf-8") == (
        "nothing personal here\n"
    )


def _drop_bad_file(repo: Path, env: dict[str, str]) -> None:
    _git(repo, env, "rm", "-q", "bad.md")
    _git(repo, env, "commit", "-q", "-m", "drop the bad file")


def _run_with(
    repo: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "tools" / "make_public_tree.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def _set_identity(env: dict[str, str], name: str, email: str) -> None:
    Path(env["GIT_CONFIG_GLOBAL"]).write_text(
        f"[user]\n\tname = {name}\n\temail = {email}\n", encoding="utf-8"
    )


def test_script_refuses_an_author_with_a_private_term(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    _set_identity(git_env, "Zzqx-Private-Term Person", "owner@example.com")
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 2
    assert "--author" in result.stderr
    assert "zzqx" not in (result.stdout + result.stderr).lower()
    assert not out.exists()


def test_script_refuses_an_author_with_a_real_address(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    _set_identity(git_env, "Test Owner", REAL_ADDRESS)
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 2
    assert "--author" in result.stderr
    assert "mailhost" not in result.stdout + result.stderr
    assert not out.exists()


def test_script_uses_an_explicit_author(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    _set_identity(git_env, "Zzqx-Private-Term Person", REAL_ADDRESS)
    out = tmp_path / "out"

    result = _run_with(
        source_repo, git_env, "--author", "Brain Maintainers <maintainers@example.com>", str(out)
    )

    assert result.returncode == 0, result.stderr
    log = _git(out, git_env, "log", "--format=%an <%ae>|%cn <%ce>").strip()
    assert log == (
        "Brain Maintainers <maintainers@example.com>|Brain Maintainers <maintainers@example.com>"
    )
    assert "Author: Brain Maintainers <maintainers@example.com>" in result.stdout


@pytest.mark.parametrize("bad", ["Nobody", "<x@example.com>", "A <not-an-address>"])
def test_script_refuses_a_malformed_author(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path, bad: str
) -> None:
    result = _run_with(source_repo, git_env, "--author", bad, str(tmp_path / "out"))

    assert result.returncode == 2
    assert "Name <email>" in result.stderr


def test_script_ignores_identity_and_date_variables_and_commits_in_utc(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    env = dict(
        git_env,
        GIT_AUTHOR_NAME="Env Author",
        GIT_AUTHOR_EMAIL="env.author@" + "mailhost.io",
        GIT_COMMITTER_NAME="Env Committer",
        GIT_COMMITTER_EMAIL="env.committer@" + "mailhost.io",
        GIT_AUTHOR_DATE="2001-02-03T04:05:06+0200",
        GIT_COMMITTER_DATE="2001-02-03T04:05:06+0200",
        TZ="Europe/Berlin",
    )
    out = tmp_path / "out"

    result = _run(source_repo, env, out)

    assert result.returncode == 0, result.stderr
    log = _git(out, git_env, "log", "--format=%an <%ae>|%cn <%ce>|%ad|%cd", "--date=raw")
    who, committer, author_date, commit_date = log.strip().split("|")
    assert who == committer == "Test Owner <owner@example.com>"
    assert author_date.endswith(" +0000") and commit_date.endswith(" +0000")
    assert not author_date.startswith("981")
    assert "Date: " in result.stdout and "+0000" in result.stdout


def test_script_refuses_a_file_name_with_a_private_term(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    (source_repo / "notes-zzqx-second-term.md").write_text("clean\n", encoding="utf-8")
    _git(source_repo, git_env, "add", "-A")
    _git(source_repo, git_env, "commit", "-q", "-m", "a file with a private name")
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 1
    assert "file names" in result.stderr
    assert "zzqx" not in result.stdout + result.stderr
    assert not (out / ".git").exists()


def test_script_refuses_an_out_dir_inside_the_source_in_another_case(
    source_repo: Path, git_env: dict[str, str]
) -> None:
    out = source_repo.parent / source_repo.name.upper() / "build"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 2
    assert "inside" in result.stderr
    assert not (source_repo / "build").exists()


def test_script_refuses_an_out_dir_inside_the_main_checkout_from_a_worktree(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    worktree = tmp_path / "worktree"
    _git(source_repo, git_env, "worktree", "add", "-q", str(worktree))
    (worktree / "user").mkdir()
    for name in ("tools/pii_blocklist.txt", "user/private_terms.txt"):
        shutil.copy2(source_repo / name, worktree / name)

    result = _run(worktree, git_env, source_repo / "build" / "out")

    assert result.returncode == 2
    assert "inside" in result.stderr
    assert not (source_repo / "build").exists()


def test_script_refuses_a_tracked_venv_or_worktrees_folder(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    (source_repo / ".venv").mkdir()
    (source_repo / ".venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    _git(source_repo, git_env, "add", "-f", ".venv/pyvenv.cfg")
    _git(source_repo, git_env, "commit", "-q", "-m", "a tracked venv")
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 2
    assert ".venv" in result.stderr
    assert not out.exists()


def test_script_never_pushes(source_repo: Path, git_env: dict[str, str], tmp_path: Path) -> None:
    _drop_bad_file(source_repo, git_env)
    remote = tmp_path / "remote.git"
    _git(tmp_path, git_env, "init", "-q", "--bare", str(remote))
    _git(source_repo, git_env, "remote", "add", "origin", str(remote))
    out = tmp_path / "out"

    result = _run(source_repo, git_env, out)

    assert result.returncode == 0, result.stderr
    assert _git(remote, git_env, "for-each-ref") == ""
    assert _git(out, git_env, "remote") == ""


def test_script_does_not_scan_an_explicit_author(
    source_repo: Path, git_env: dict[str, str], tmp_path: Path
) -> None:
    _drop_bad_file(source_repo, git_env)
    out = tmp_path / "out"

    result = _run_with(
        source_repo, git_env, "--author", f"Test Owner <{REAL_ADDRESS}>", str(out)
    )

    assert result.returncode == 0, result.stderr
    assert _git(out, git_env, "log", "--format=%ae").strip() == REAL_ADDRESS
