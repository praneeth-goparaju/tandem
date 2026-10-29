from __future__ import annotations

import httpx
import pytest

from src.brain.mcp.tools._icloud import (
    _assert_icloud_request,
    _HostScopedBasicAuth,
    _reject_unsafe_redirect,
    assert_icloud_host,
    is_icloud_host,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://p15-caldav.icloud.com/u/calendars/home/event.ics",
        "https://contacts.icloud.com/x",
        "https://caldav.icloud.com/",
        "https://imap.mail.me.com/",
        "https://p55-CONTACTS.iCloud.COM/x",
        "https://caldav.icloud.com./",
        "https://p15-caldav.icloud.com.cn/x",
    ],
)
def test_assert_icloud_host_accepts_icloud(url: str) -> None:
    assert assert_icloud_host(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/x",
        "https://icloud.com.evil.com/x",
        "https://eviCloud.com/x",
        "//evil.com/x",
        "file:///etc/passwd",
        "ftp://attacker.test/",
        "http://contacts.icloud.com.attacker.test/",
        "http://contacts.icloud.com/x",
        "https://user:test@contacts.icloud.com/x",
        "https://contacts.icloud.com:8443/x",
    ],
)
def test_assert_icloud_host_rejects_non_icloud(url: str) -> None:
    with pytest.raises(ValueError):
        assert_icloud_host(url)


def test_is_icloud_host_handles_empty() -> None:
    assert not is_icloud_host(None)
    assert not is_icloud_host("")


def test_host_scoped_auth_attaches_to_icloud() -> None:
    auth = _HostScopedBasicAuth("apple_id_user", "app-pw")
    req = httpx.Request("GET", "https://contacts.icloud.com/x")
    list(auth.auth_flow(req))
    assert req.headers["Authorization"].startswith("Basic ")


def test_host_scoped_auth_drops_on_cross_origin() -> None:
    auth = _HostScopedBasicAuth("apple_id_user", "app-pw")
    for hostile in (
        "https://evil.com/x",
        "https://icloud.com.evil.com/x",
        "http://contacts.icloud.com.attacker.test/",
        "http://contacts.icloud.com/x",
    ):
        req = httpx.Request("GET", hostile)
        list(auth.auth_flow(req))
        assert "Authorization" not in req.headers, hostile


def test_host_scoped_auth_removes_inherited_header_on_downgrade() -> None:
    auth = _HostScopedBasicAuth("apple_id_user", "app-pw")
    req = httpx.Request(
        "GET",
        "http://contacts.icloud.com/x",
        headers={"Authorization": "Basic inherited"},
    )
    list(auth.auth_flow(req))
    assert "Authorization" not in req.headers


def test_carddav_redirect_hook_blocks_https_downgrade() -> None:
    request = httpx.Request("GET", "https://contacts.icloud.com/start")
    response = httpx.Response(
        302,
        headers={"Location": "http://contacts.icloud.com/next"},
        request=request,
    )

    with pytest.raises(ValueError, match="non-HTTPS"):
        _reject_unsafe_redirect(response)


def test_carddav_redirect_hook_accepts_relative_icloud_target() -> None:
    request = httpx.Request("GET", "https://contacts.icloud.com/start")
    response = httpx.Response(
        302,
        headers={"Location": "/next"},
        request=request,
    )

    assert _reject_unsafe_redirect(response) is None


def test_caldav_request_hook_blocks_non_icloud_url() -> None:
    request = httpx.Request("PROPFIND", "https://metadata.invalid/internal")
    with pytest.raises(ValueError, match="non-iCloud"):
        _assert_icloud_request(request)


def test_caldav_client_installs_host_and_redirect_hooks(monkeypatch) -> None:
    from src.brain.mcp.tools import _icloud

    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "owner@example.test")
    monkeypatch.setattr(_icloud, "ICLOUD_APP_PASSWORD", "test-password")
    client = _icloud.caldav_client()

    assert client.session.trust_env is False
    assert _assert_icloud_request in client.session.hooks["pre_request"]
    assert _reject_unsafe_redirect in client.session.hooks["response"]
