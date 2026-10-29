from pathlib import Path

from src.brain.doctor import checks


def test_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert checks.check_gmail_imap_reachable(None, Path(".")) == []


def test_auth_failure_returns_issue(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "bad")
    import imaplib

    class FakeIMAP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        def logout(self): pass
    monkeypatch.setattr(checks.imaplib, "IMAP4_SSL", FakeIMAP, raising=False)
    issues = checks.check_gmail_imap_reachable(None, Path("."))
    assert len(issues) == 1
    assert "auth failed" in issues[0].message.lower()
