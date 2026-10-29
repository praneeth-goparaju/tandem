import pytest

from src.brain.mcp.tools import _icloud
from src.brain.mcp.tools import _mail_providers as mp


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        mp.get_provider("outlook")


def test_gmail_provider_fields(monkeypatch):
    monkeypatch.setattr(mp, "GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setattr(mp, "GMAIL_APP_PASSWORD", "secret")
    p = mp.get_provider("gmail")
    assert p.imap_host == "imap.gmail.com"
    assert p.imap_port == 993
    assert p.login_user == "me@gmail.com"
    assert p.drafts_mailbox == "[Gmail]/Drafts"
    assert p.uses_gmail_labels is True


def test_gmail_not_configured_raises(monkeypatch):
    monkeypatch.setattr(mp, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(mp, "GMAIL_APP_PASSWORD", "")
    with pytest.raises(mp.MailNotConfigured):
        mp.get_provider("gmail")


def test_icloud_provider_uses_imap_user(monkeypatch):
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@example.org")
    monkeypatch.setattr(_icloud, "ICLOUD_APP_PASSWORD", "secret")
    monkeypatch.setattr(mp, "ICLOUD_IMAP_USER", "me@icloud.com")
    p = mp.get_provider("icloud")
    assert p.imap_host == "imap.mail.me.com"
    assert p.login_user == "me@icloud.com"
    assert p.uses_gmail_labels is False


def test_icloud_provider_password_from_keychain(monkeypatch):
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(_icloud, "ICLOUD_APP_PASSWORD", "")
    monkeypatch.setattr(_icloud, "_keychain_password", lambda apple_id: "kc-secret")
    monkeypatch.setattr(mp, "ICLOUD_IMAP_USER", "")
    p = mp.get_provider("icloud")
    assert p.password == "kc-secret"
    assert p.login_user == "me@icloud.com"


def test_icloud_not_configured_names_both_options(monkeypatch):
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(_icloud, "ICLOUD_APP_PASSWORD", "")
    with pytest.raises(mp.MailNotConfigured) as exc:
        mp.get_provider("icloud")
    assert ".env" in str(exc.value)
    assert "brain-icloud-app-password" in str(exc.value)
