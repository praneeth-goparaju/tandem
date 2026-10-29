from src.brain.mcp.tools import icloud_read


def test_session_for_default_is_icloud(monkeypatch):
    called = {}
    def fake_icloud_session(readonly=True):
        called["icloud"] = True
        from contextlib import nullcontext
        return nullcontext(None)
    monkeypatch.setattr(icloud_read, "_icloud_imap_session", fake_icloud_session)
    with icloud_read._session_for("icloud"):
        pass
    assert called.get("icloud") is True


def test_session_for_gmail_uses_provider(monkeypatch):
    called = {}
    def fake_get_provider(name):
        called["name"] = name
        return object()
    def fake_generic_session(provider, readonly=True):
        called["generic"] = True
        from contextlib import nullcontext
        return nullcontext(None)
    monkeypatch.setattr(icloud_read, "_get_provider", fake_get_provider)
    monkeypatch.setattr(icloud_read, "_generic_imap_session", fake_generic_session)
    with icloud_read._session_for("gmail"):
        pass
    assert called["name"] == "gmail"
    assert called.get("generic") is True
