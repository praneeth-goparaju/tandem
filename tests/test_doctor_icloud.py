from __future__ import annotations

from src.brain.doctor import CHECKS
from src.brain.doctor import checks as _doctor_checks  # noqa: F401

_ICLOUD_CHECK_NAMES = (
    "check_icloud_creds",
    "check_icloud_caldav_reachable",
    "check_icloud_imap_reachable",
    "check_icloud_carddav_reachable",
)


def test_creds_check_is_warn_severity() -> None:
    by_name = {c.name: c for c in CHECKS}
    assert by_name["check_icloud_creds"].severity == "warn"


def test_reachability_checks_are_warn_severity() -> None:
    by_name = {c.name: c for c in CHECKS}
    for name in (
        "check_icloud_caldav_reachable",
        "check_icloud_imap_reachable",
        "check_icloud_carddav_reachable",
    ):
        assert by_name[name].severity == "warn", (
            f"{name} must be warn severity so a flaky network at boot doesn't "
            f"block the scheduler"
        )


def test_checks_no_op_when_creds_missing(monkeypatch) -> None:
    monkeypatch.delenv("ICLOUD_APPLE_ID", raising=False)
    monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
    by_name = {c.name: c for c in CHECKS}
    for name in (
        "check_icloud_creds",
        "check_icloud_caldav_reachable",
        "check_icloud_imap_reachable",
        "check_icloud_carddav_reachable",
    ):
        check = by_name[name]
        assert check.fn(None, None) == [], f"{name} should no-op without creds"


def test_creds_check_flags_half_configured(monkeypatch) -> None:
    by_name = {c.name: c for c in CHECKS}
    monkeypatch.setenv("ICLOUD_APPLE_ID", "u@example.com")
    monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
    issues = by_name["check_icloud_creds"].fn(None, None)
    assert len(issues) == 1
    assert "ICLOUD_APP_PASSWORD" in issues[0].message
    assert "brain-icloud-app-password" in issues[0].message


def test_creds_check_accepts_keychain_password(monkeypatch) -> None:
    from src.brain.mcp.tools import _icloud

    by_name = {c.name: c for c in CHECKS}
    monkeypatch.setenv("ICLOUD_APPLE_ID", "u@example.com")
    monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
    monkeypatch.setattr(_icloud, "_keychain_password", lambda apple_id: "kc-secret")
    assert by_name["check_icloud_creds"].fn(None, None) == []
