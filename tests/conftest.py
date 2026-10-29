from __future__ import annotations

import importlib
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def _is_local(host: object) -> bool:
    return not isinstance(host, str) or host in _LOCAL_HOSTS or host.startswith("/")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    connect, getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local(host):
            raise AssertionError(f"a test tried to connect to {address!r}")
        return connect(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            raise AssertionError(f"a test tried to resolve {host!r}")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud._keychain_password", lambda apple_id: ""
    )


@pytest.fixture(autouse=True)
def _berlin_timezone(monkeypatch):
    """The suite's fixed zone, so no test depends on the owner's ``.env`` or the Mac.

    ``TZ`` is pinned too, so the tests that compare with the local date give
    the same answer on a UTC runner late in the evening.
    """
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    monkeypatch.setattr("src.orchestrator.config.TIMEZONE", "Europe/Berlin")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    time.tzset()


@pytest.fixture(autouse=True)
def _isolate_local_env(monkeypatch):
    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "0")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("BRAIN_SLACK_OFF", "0")
    from src.brain.mcp import _common

    _common.notifier.cache_clear()


@pytest.fixture(autouse=True)
def _home_tab_reads_no_live_calendar(monkeypatch):
    from src.brain.followups import home

    monkeypatch.setattr(home, "_today_lines", lambda today: [])


@pytest.fixture(autouse=True)
def _reminders_access_granted(monkeypatch):
    monkeypatch.setattr(
        "src.brain.mcp.tools._icloud.reminders_access_granted", lambda: True
    )


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_file = tmp_path / "vault.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_file))
    monkeypatch.setenv("VAULT_DB_PATH", str(db_file))

    import src.db as dbmod
    import src.orchestrator.config as cfg

    importlib.reload(cfg)
    importlib.reload(dbmod)

    yield dbmod

    if hasattr(dbmod, "_local") and getattr(dbmod._local, "conn", None) is not None:
        try:
            dbmod._local.conn.close()
        except Exception:
            pass
        dbmod._local.conn = None

    importlib.reload(cfg)
    importlib.reload(dbmod)


@pytest.fixture
def parsed_parent():
    from src.brain.mcp.tools._mail_quote import ParsedParent

    def _make(**overrides):
        base = dict(
            msgid="<a@b>", references="", from_="MyFactura <noreply@invoice.test>",
            subject="Mahnung", date="Mon, 1 Jan 2026", body="please pay",
            recipients=("alice@icloud.com",), x_icloud_hme="",
        )
        base.update(overrides)
        return ParsedParent(**base)

    return _make


@pytest.fixture
def isolated_vault(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    import src.orchestrator.config as cfg
    importlib.reload(cfg)
    yield tmp_path
    importlib.reload(cfg)
