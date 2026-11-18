from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher

from src.brain.connector import __main__ as cli
from src.brain.connector.auth.provider import sign_in_set_up
from src.brain.connector.auth.store import AuthStore
from src.brain.connector.config import ConnectorConfig
from src.brain.connector.server import AUTH_DB

PASSWORD = "a long password for the vault"


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConnectorConfig:
    cfg = ConnectorConfig(
        vault_path=tmp_path / "Vault",
        data_dir=tmp_path,
        public_url="https://vault.example.com",
        allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    return cfg


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    replies: Iterator[str] = iter(answers)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(replies))


def test_set_password_twice(
    config: ConnectorConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, PASSWORD, PASSWORD)

    assert cli.main(["set-password"]) == 0

    store = AuthStore(config.data_dir / AUTH_DB)
    stored = store.get_setting("password_hash", "owner")
    assert stored is not None and PasswordHasher().verify(stored, PASSWORD)
    assert PASSWORD not in capsys.readouterr().out


@pytest.mark.parametrize(
    "answers",
    [(PASSWORD, PASSWORD + "x"), ("short", "short"), ("fifteen chars!!", "fifteen chars!!")],
)
def test_set_password_refused(
    config: ConnectorConfig, monkeypatch: pytest.MonkeyPatch, answers: tuple[str, str]
) -> None:
    _answers(monkeypatch, *answers)

    assert cli.main(["set-password"]) == 1
    assert AuthStore(config.data_dir / AUTH_DB).get_setting("password_hash", "owner") is None


def test_totp_setup_prints_only_the_address(
    config: ConnectorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["totp-setup"]) == 0

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("otpauth://totp/")


def test_revoke_all(
    config: ConnectorConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, PASSWORD, PASSWORD)
    cli.main(["set-password"])
    cli.main(["totp-setup"])
    capsys.readouterr()

    assert cli.main(["revoke-all"]) == 0
    assert capsys.readouterr().out.strip() == "done"
    assert sign_in_set_up(AuthStore(config.data_dir / AUTH_DB), "owner")


@pytest.fixture
def two_people(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_people: Callable[..., Any]
) -> ConnectorConfig:
    cfg = ConnectorConfig(
        vault_path=tmp_path / "Vault",
        data_dir=tmp_path,
        public_url="https://vault.example.com",
        allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
        people=make_people(),
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    return cfg


def test_with_two_people_the_person_must_be_named(
    two_people: ConnectorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in ("set-password", "totp-setup"):
        assert cli.main([command]) == 2
        assert "--person" in capsys.readouterr().err

    assert cli.main(["totp-setup", "--person", "pat"]) == 2
    err = capsys.readouterr().err
    assert "pat" in err and "alex, sam" in err


def test_set_password_for_one_person(
    two_people: ConnectorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answers(monkeypatch, PASSWORD, PASSWORD)

    assert cli.main(["set-password", "--person", "sam"]) == 0

    store = AuthStore(two_people.data_dir / AUTH_DB)
    stored = store.get_setting("password_hash", "sam")
    assert stored is not None and PasswordHasher().verify(stored, PASSWORD)
    assert store.get_setting("password_hash", "alex") is None


def test_totp_setup_names_the_person(
    two_people: ConnectorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["totp-setup", "--person", "sam"]) == 0

    (line,) = capsys.readouterr().out.strip().splitlines()
    assert line.startswith("otpauth://totp/Vault:Sam?")
    assert AuthStore(two_people.data_dir / AUTH_DB).get_setting("totp_secret", "sam") is not None


def test_revoke_all_for_one_person_leaves_the_other_signed_in(
    two_people: ConnectorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    store = AuthStore(two_people.data_dir / AUTH_DB)
    store.add_tokens("c", [], "https://vault.example.com/mcp", "alex-a", "alex-r", "alex")
    store.add_tokens("c", [], "https://vault.example.com/sam/mcp", "sam-a", "sam-r", "sam")

    assert cli.main(["revoke-all", "--person", "sam"]) == 0

    assert "Revoked 2 tokens and codes." in capsys.readouterr().out
    assert store.get_token("sam-a", "access") is None
    assert store.get_token("alex-a", "access") is not None
    assert cli.main(["revoke-all"]) == 0
    assert store.get_token("alex-a", "access") is None


def test_one_person_needs_no_person_option(
    config: ConnectorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["totp-setup", "--person", "owner"]) == 0
    assert capsys.readouterr().out.startswith("otpauth://totp/Vault:owner?")
