from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import check_pii, gen_pii_blocklist

REPO_ROOT = Path(__file__).resolve().parent.parent
# Built in pieces so this file does not trip the whole-tree scan it tests.
USERS_PATH = "/Users" + "/jsmith"
HOME_PATH = "/home" + "/zed"
OTHER_EMAIL = "bob" + "@corp.io"
ZELDA_EMAIL = "zelda" + "@corp.io"


def test_empty_blocklist_fails_closed(tmp_path, monkeypatch) -> None:
    blocklist = tmp_path / "pii_blocklist.txt"
    blocklist.write_text("# comments only\n", encoding="utf-8")
    blocklist.chmod(0o600)
    monkeypatch.setattr(check_pii, "BLOCKLIST", blocklist)

    with pytest.raises(SystemExit) as exc:
        check_pii.load_blocklist()

    assert exc.value.code == 1


def test_symlink_blocklist_fails_closed(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private-person@example.test\n", encoding="utf-8")
    outside.chmod(0o600)
    blocklist = tmp_path / "pii_blocklist.txt"
    blocklist.symlink_to(outside)
    monkeypatch.setattr(check_pii, "BLOCKLIST", blocklist)

    with pytest.raises(SystemExit) as exc:
        check_pii.load_blocklist()

    assert exc.value.code == 1


def test_match_report_does_not_echo_private_value(monkeypatch, capsys) -> None:
    private_value = "private-person@example.test"
    monkeypatch.setattr(check_pii, "load_blocklist", lambda: [private_value])
    monkeypatch.setattr(
        check_pii,
        "staged_added_lines",
        lambda: [("README.md", 7, f"contact {private_value}")],
    )

    assert check_pii.main() == 1
    output = capsys.readouterr().err
    assert "README.md:7" in output
    assert private_value not in output


def test_generated_blocklist_uses_high_confidence_identifiers() -> None:
    tokens = gen_pii_blocklist.collect_tokens(
        {
            "user": {
                "name": "Private Person",
                "employer": "Public Company",
                "locations": ["Public City"],
                "target_companies": ["Another Company"],
                "e164": "+49123456789",
                "username": "private-account",
                "email": "private.person@example.invalid",
            },
            "family": {"partner": {"full_name": "Private Partner"}},
            "past_employers": ["Former Public Company"],
        }
    )

    assert "Private Person" in tokens
    assert "Private Partner" in tokens
    assert "private.person@example.invalid" in tokens
    assert "+49123456789" in tokens
    assert "private-account" in tokens
    assert "Private" not in tokens
    assert "Person" not in tokens
    assert "Public Company" not in tokens
    assert "Public City" not in tokens
    assert "Another Company" not in tokens
    assert "Former Public Company" not in tokens


def test_scan_flags_absolute_home_path() -> None:
    findings = check_pii.scan_text(f"see {USERS_PATH}/brain/x", terms=[], path="a.md")

    assert findings == ["a.md:1: absolute-home-path"]


@pytest.mark.parametrize(
    "text",
    ["~/brain", "$HOME/brain", "__HOME__/brain", "/Users/__HOME__"],
)
def test_scan_allows_placeholders(text: str) -> None:
    assert check_pii.scan_text(text, terms=[], path="a.md") == []


@pytest.mark.parametrize(
    "name", ["me", "you", "alice", "bob", "example", "user", "test", "ME", "Alice"]
)
def test_scan_allows_placeholder_usernames(name: str) -> None:
    for root in ("/Users", "/home"):
        text = f"{root}/{name}/brain and {root}/{name}"
        assert check_pii.scan_text(text, terms=[], path="a.md") == []


@pytest.mark.parametrize("name", ["jsmith", "meg", "alicea", "users", "tester", "me.x"])
def test_scan_flags_other_usernames(name: str) -> None:
    text = "/Users" + f"/{name}/brain"
    assert check_pii.scan_text(text, terms=[], path="a.md") == [
        "a.md:1: absolute-home-path"
    ]


def test_scan_flags_email_but_allows_examples() -> None:
    assert check_pii.scan_text(f"mail {OTHER_EMAIL}", terms=[], path="a.md") == [
        "a.md:1: email"
    ]
    for ok in (
        "you@example.com",
        "noreply@anthropic.com",
        "anyone@example.com",
        "me@<name>.example.com",
        "me@mail.example.com",
        "a@example.org",
        "a@example.net",
        "a@mail.example.org",
        "a@foo.test",
        "a@example.invalid",
        "a@app.localhost",
        "a@foo.example",
        "A@EXAMPLE.ORG",
    ):
        assert check_pii.scan_text(ok, terms=[], path="a.md") == []


def test_scan_allows_placeholder_addresses_at_service_domains() -> None:
    for ok in (
        "alice@gmail.com",
        "someone@icloud.com",
        "me@me.com",
        "you@mac.com",
        "user+news@gmail.com",
        "someone@privaterelay.appleid.com",
        "noreply@users.noreply.github.com",
        "no-reply@github.com",
        "noreply@anthropic.com",
        "TEST@GMAIL.COM",
    ):
        assert check_pii.scan_text(ok, terms=[], path="a.md") == []


@pytest.mark.parametrize(
    "local", ["jsmith", "alice.smith", "someone2", "x", "relay", "test.user"]
)
def test_scan_flags_other_addresses_at_service_domains(local: str) -> None:
    for domain in ("gmail.com", "icloud.com", "users.noreply.github.com"):
        text = local + "@" + domain
        assert check_pii.scan_text(text, terms=[], path="a.md") == ["a.md:1: email"]


@pytest.mark.parametrize(
    "text",
    [
        "a@" + "notexample.com",
        "a@" + "example.com.evil.io",
        "a@" + "example.org.evil.io",
        "a@" + "notgmail.com",
        "a@" + "gmail.com.evil.io",
        "a@" + "icloud.co",
    ],
)
def test_scan_flags_lookalike_domains(text: str) -> None:
    assert check_pii.scan_text(text, terms=[], path="a.md") == ["a.md:1: email"]


def test_scan_private_term_folds_case_and_unicode() -> None:
    assert check_pii.scan_text("STRASSE 5", terms=["Straße"], path="a.md") == [
        "a.md:1: private-term"
    ]
    decomposed = "Cafe\u0301 Noir"
    assert check_pii.scan_text(decomposed, terms=["caf\u00e9"], path="a.md") == [
        "a.md:1: private-term"
    ]


def test_scan_reports_one_finding_per_hit_without_echoing_terms() -> None:
    findings = check_pii.scan_text(
        f"Zelda at {HOME_PATH} and {ZELDA_EMAIL}", terms=["Zelda"], path="n.md"
    )

    assert sorted(findings) == [
        "n.md:1: absolute-home-path",
        "n.md:1: email",
        "n.md:1: private-term",
    ]
    assert not any("Zelda" in f or "corp.io" in f for f in findings)


def test_tracked_files_skip_private_and_binary_files(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "keep.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"ab\x00cd")
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "secret.md").write_text("x\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("x\n", encoding="utf-8")
    (tmp_path / ".superpowers").mkdir()
    (tmp_path / ".superpowers" / "plan.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "personal_terms_public.txt").write_text("m\n", encoding="utf-8")
    git("add", "-f", ".")

    names = [p.relative_to(tmp_path).as_posix() for p in check_pii.tracked_files(tmp_path)]

    assert names == ["keep.md", "tools/personal_terms_public.txt"]


def test_tree_mode_without_private_never_reads_the_blocklist(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(check_pii, "BLOCKLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(check_pii, "tracked_files", lambda root: [])
    monkeypatch.setattr(check_pii, "tracked_private_paths", lambda root: [])

    assert check_pii.main(["--tree"]) == 0


def test_tree_mode_reports_findings_and_exits_1(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    leaky = tmp_path / "doc.md"
    leaky.write_text(f"one\nsee {USERS_PATH}/x\n", encoding="utf-8")
    monkeypatch.setattr(check_pii, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_pii, "tracked_files", lambda root: [leaky])
    monkeypatch.setattr(check_pii, "tracked_private_paths", lambda root: [])

    assert check_pii.main(["--tree"]) == 1
    assert "doc.md:2: absolute-home-path" in capsys.readouterr().err


def test_tree_private_mode_reads_blocklist_and_user_terms(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "private_terms.txt").write_text(
        "# mine\nQuillfeather\n", encoding="utf-8"
    )
    blocklist = tmp_path / "pii_blocklist.txt"
    blocklist.write_text("Marmalade Person\n", encoding="utf-8")
    blocklist.chmod(0o600)
    doc = tmp_path / "doc.md"
    doc.write_text("quillfeather\nmarmalade person\nclean\n", encoding="utf-8")
    monkeypatch.setattr(check_pii, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_pii, "BLOCKLIST", blocklist)
    monkeypatch.setattr(check_pii, "tracked_files", lambda root: [doc])
    monkeypatch.setattr(check_pii, "tracked_private_paths", lambda root: [])

    assert check_pii.main(["--tree", "--private"]) == 1
    err = capsys.readouterr().err
    assert "doc.md:1: private-term" in err
    assert "doc.md:2: private-term" in err
    assert "doc.md:3" not in err
    assert "uillfeather" not in err
    assert "armalade" not in err


def test_tree_private_mode_works_without_a_blocklist(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "private_terms.txt").write_text(
        "Quillfeather Jones\n", encoding="utf-8"
    )
    doc = tmp_path / "doc.md"
    doc.write_text("quillfeather jones\n", encoding="utf-8")
    monkeypatch.setattr(check_pii, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_pii, "BLOCKLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(check_pii, "tracked_files", lambda root: [doc])
    monkeypatch.setattr(check_pii, "tracked_private_paths", lambda root: [])

    assert check_pii.main(["--tree", "--private"]) == 1
    err = capsys.readouterr().err
    assert "doc.md:1: private-term" in err
    assert "Refusing commit" not in err


def test_tree_private_mode_still_rejects_an_unsafe_blocklist(
    tmp_path: Path, monkeypatch
) -> None:
    blocklist = tmp_path / "pii_blocklist.txt"
    blocklist.write_text("Marmalade Person\n", encoding="utf-8")
    blocklist.chmod(0o644)
    monkeypatch.setattr(check_pii, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_pii, "BLOCKLIST", blocklist)
    monkeypatch.setattr(check_pii, "tracked_files", lambda root: [])
    monkeypatch.setattr(check_pii, "tracked_private_paths", lambda root: [])

    with pytest.raises(SystemExit) as exc:
        check_pii.main(["--tree", "--private"])

    assert exc.value.code == 1


def test_tracked_user_path_is_its_own_finding(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "x.yaml").write_text("name: nobody\n", encoding="utf-8")
    (tmp_path / "ok.md").write_text("fine\n", encoding="utf-8")
    git("add", "-f", ".")
    monkeypatch.setattr(check_pii, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_pii, "PUBLIC_TERMS", tmp_path / "none.txt")
    (tmp_path / "none.txt").write_text("# none\n", encoding="utf-8")

    assert check_pii.tracked_private_paths(tmp_path) == ["user/x.yaml"]
    assert check_pii.main(["--tree"]) == 1
    err = capsys.readouterr().err
    assert "user/x.yaml:0: tracked-private-overlay" in err
    assert "user/x.yaml:1" not in err


def test_public_terms_file_holds_only_comments() -> None:
    terms = check_pii.load_terms_file(REPO_ROOT / "tools" / "personal_terms_public.txt")

    assert terms == []


def test_tree_mode_passes_on_this_repo() -> None:
    terms = check_pii.load_terms_file(REPO_ROOT / "tools" / "personal_terms_public.txt")
    findings: list[str] = []
    for path in check_pii.tracked_files(REPO_ROOT):
        text = path.read_text(encoding="utf-8", errors="replace")
        findings += check_pii.scan_text(
            text, terms=terms, path=path.relative_to(REPO_ROOT).as_posix()
        )

    assert findings == []


def test_root_argument_walks_a_tree_without_git(tmp_path: Path, capsys) -> None:
    (tmp_path / "ok.md").write_text("fine\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "leak.md").write_text(f"see {USERS_PATH}/x\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "cfg").write_text(f"{USERS_PATH}/y\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"ab\x00cd")
    assert not (tmp_path / ".git" / "HEAD").exists()

    assert check_pii.main(["--tree", "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err

    assert "docs/leak.md:1: absolute-home-path" in err
    assert ".git" not in err
    assert "ok.md" not in err


def test_root_argument_passes_a_clean_tree_and_flags_user_folder(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "ok.md").write_text("fine\n", encoding="utf-8")

    assert check_pii.main(["--tree", "--root", str(tmp_path)]) == 0

    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "profile.yaml").write_text("x\n", encoding="utf-8")

    assert check_pii.main(["--tree", "--root", str(tmp_path)]) == 1
    assert "user/profile.yaml:0: tracked-private-overlay" in capsys.readouterr().err


def test_root_argument_needs_tree_mode() -> None:
    with pytest.raises(SystemExit):
        check_pii.main(["--root", "/nonexistent"])


def test_root_argument_is_resolved_through_symlinks(tmp_path: Path, capsys) -> None:
    real = tmp_path / "real"
    (real / "tools").mkdir(parents=True)
    (real / "tools" / "personal_terms_public.txt").write_text("# none\n", encoding="utf-8")
    (real / "leak.md").write_text(f"see {USERS_PATH}/x\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)

    assert check_pii.main(["--tree", "--root", str(link)]) == 1
    assert "leak.md:1: absolute-home-path" in capsys.readouterr().err


def test_private_mode_says_when_no_private_term_file_exists(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(check_pii, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_pii, "BLOCKLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(check_pii, "tracked_files", lambda root: [])
    monkeypatch.setattr(check_pii, "tracked_private_paths", lambda root: [])

    assert check_pii.main(["--tree", "--private"]) == 0
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "no private term file" in err[0]
    assert "only the public rules" in err[0]


def test_private_mode_is_quiet_when_a_private_term_file_exists(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "user").mkdir()
    (tmp_path / "user" / "private_terms.txt").write_text("Quillfeather\n", encoding="utf-8")
    monkeypatch.setattr(check_pii, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_pii, "BLOCKLIST", tmp_path / "missing.txt")
    monkeypatch.setattr(check_pii, "tracked_files", lambda root: [])
    monkeypatch.setattr(check_pii, "tracked_private_paths", lambda root: [])

    assert check_pii.main(["--tree", "--private"]) == 0
    assert capsys.readouterr().err == ""


def test_root_argument_never_follows_symlinks(tmp_path: Path, capsys) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text(f"see {USERS_PATH}/x\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.md").write_text("fine\n", encoding="utf-8")
    (root / "linked folder").symlink_to(outside)
    (root / "linked.md").symlink_to(outside / "leak.md")

    assert check_pii.main(["--tree", "--root", str(root)]) == 0
    assert "leak.md" not in capsys.readouterr().err


def test_root_argument_takes_the_private_terms_from_this_repository(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    (repo / "user").mkdir(parents=True)
    (repo / "user" / "private_terms.txt").write_text("Quillfeather\n", encoding="utf-8")
    root = tmp_path / "root"
    (root / "user").mkdir(parents=True)
    (root / "doc.md").write_text("quillfeather\n", encoding="utf-8")
    monkeypatch.setattr(check_pii, "REPO_ROOT", repo)
    monkeypatch.setattr(check_pii, "BLOCKLIST", tmp_path / "missing.txt")

    assert check_pii.main(["--tree", "--private", "--root", str(root)]) == 1
    assert "doc.md:1: private-term" in capsys.readouterr().err
