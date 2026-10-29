from __future__ import annotations

import base64
import contextlib
import imaplib
import re
import subprocess
from collections.abc import Generator, Iterator
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import httpx

from src.orchestrator.config import (
    ICLOUD_APP_PASSWORD,
    ICLOUD_APPLE_ID,
    ICLOUD_IMAP_USER,
)

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
ICLOUD_CARDDAV_URL = "https://contacts.icloud.com"
ICLOUD_IMAP_HOST = "imap.mail.me.com"
ICLOUD_IMAP_PORT = 993

NETWORK_TIMEOUT = 15.0
CARDDAV_NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:carddav",
}

_ICLOUD_HOST_SUFFIXES = (".icloud.com", ".icloud.com.cn", ".me.com")


class ICloudNotConfigured(RuntimeError):
    ...


KEYCHAIN_SERVICE = "brain-icloud-app-password"
_KEYCHAIN_TIMEOUT = 5.0
_keychain_passwords: dict[str, str] = {}


def _keychain_password(apple_id: str) -> str:
    """The app password from the login Keychain, or "" when there is none.

    Asks the `security` tool once per process for each Apple ID and keeps the
    answer in memory. A miss is not kept, so adding the item takes effect
    without a restart.
    """
    cached = _keychain_passwords.get(apple_id)
    if cached:
        return cached
    try:
        result = subprocess.run(  # noqa: S603
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", apple_id, "-w"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=_KEYCHAIN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    password = result.stdout.strip("\r\n")
    if password:
        _keychain_passwords[apple_id] = password
    return password


def require_creds() -> tuple[str, str]:
    if ICLOUD_APPLE_ID:
        if ICLOUD_APP_PASSWORD:
            return ICLOUD_APPLE_ID, ICLOUD_APP_PASSWORD
        password = _keychain_password(ICLOUD_APPLE_ID)
        if password:
            return ICLOUD_APPLE_ID, password
    raise ICloudNotConfigured(
        "iCloud is not set up. Either set ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD "
        "in .env, or set ICLOUD_APPLE_ID in .env and put the app-specific password "
        f"in the login Keychain as item {KEYCHAIN_SERVICE}: run "
        f"`security add-generic-password -s {KEYCHAIN_SERVICE} -a <apple id> -w` "
        "and type the password at the prompt. See docs/INTEGRATIONS.md."
    )


def is_icloud_host(host: str | None) -> bool:
    if not host:
        return False
    h = host.lower()
    if h.endswith("."):
        h = h[:-1]
    return any(h == suf[1:] or h.endswith(suf) for suf in _ICLOUD_HOST_SUFFIXES)


def assert_icloud_host(url: str) -> str:
    """Reject URLs that don't point at iCloud. Returns the URL unchanged on success.

    Use this on every URL that came from an action payload, agent input, or
    server-side CardDAV/CalDAV `href` before handing it to httpx / caldav —
    `urljoin('https://contacts.icloud.com', '//evil/x')` returns `https://evil/x`
    and httpx with bound auth will leak the Basic-auth header to that host.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"refusing non-HTTPS iCloud URL: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"refusing URL with userinfo: {url!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"refusing URL with invalid port: {url!r}") from exc
    if port not in (None, 443):
        raise ValueError(f"refusing non-HTTPS port {port!r} in URL: {url!r}")
    if not is_icloud_host(parts.hostname):
        raise ValueError(f"refusing non-iCloud host {parts.hostname!r} in URL {url!r}")
    return url


_IMAP_FORBIDDEN_CHARS = ("\r", "\n", "\x00")
_SINGLE_IMAP_UID_RE = re.compile(r"^[1-9][0-9]*$")


def escape_imap_str(s: str) -> str:
    """Escape `\\` and `"` for an IMAP-quoted string. Reject CR/LF/NUL outright.

    IMAP commands are line-terminated; `\\r\\n` in a quoted argument lets an
    attacker (including LLM-routed mail content that loops back into a search
    query) splice additional commands. We don't try to escape — we refuse.
    """
    if not isinstance(s, str):
        raise TypeError(f"escape_imap_str expects str, got {type(s).__name__}")
    for ch in _IMAP_FORBIDDEN_CHARS:
        if ch in s:
            raise ValueError("control character in IMAP argument")
    return s.replace("\\", "\\\\").replace('"', '\\"')


def validate_single_uid(value: str) -> str:
    if not isinstance(value, str) or not _SINGLE_IMAP_UID_RE.fullmatch(value):
        raise ValueError("invalid uid: expected one positive numeric IMAP UID")
    return value


def validate_mailbox_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("mailbox name must be a non-empty string")
    for ch in _IMAP_FORBIDDEN_CHARS + ('"', "\\"):
        if ch in name:
            raise ValueError(f"forbidden char {ch!r} in mailbox name")
    return name


class _HostScopedBasicAuth(httpx.Auth):
    def __init__(self, user: str, password: str) -> None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._header = f"Basic {token}"

    def auth_flow(self, request) -> Generator[Any, Any, None]:
        request.headers.pop("Authorization", None)
        if (
            request.url.scheme == "https"
            and request.url.port in (None, 443)
            and is_icloud_host(request.url.host)
        ):
            request.headers["Authorization"] = self._header
        yield request


def _reject_unsafe_redirect(response) -> None:
    location = response.headers.get("location")
    if not location or response.status_code not in {301, 302, 303, 307, 308}:
        return
    target = urljoin(str(response.request.url), location)
    assert_icloud_host(target)


def _assert_icloud_request(request):
    assert_icloud_host(str(request.url))
    return request


def caldav_client():
    import caldav

    apple_id, app_pw = require_creds()
    # caldav exposes DAVClient through a dynamic module attribute; its package
    # metadata types that attribute as object even though it is a constructor.
    dav_client_cls = cast(Any, caldav.DAVClient)
    client = dav_client_cls(
        url=ICLOUD_CALDAV_URL,
        username=apple_id,
        password=app_pw,
        timeout=NETWORK_TIMEOUT,
        enable_rfc6764=False,
    )
    # python-caldav uses niquests internally and follows redirects. Hooks run
    # for every prepared request and every redirect response, keeping all hops
    # on authenticated HTTPS iCloud origins (the same policy as CardDAV).
    client.session.trust_env = False
    client.session.hooks["pre_request"].append(_assert_icloud_request)
    client.session.hooks["response"].append(_reject_unsafe_redirect)
    return client


@contextlib.contextmanager
def imap_session() -> Iterator[imaplib.IMAP4_SSL]:
    """Context-managed IMAP connection. Auto-LOGIN, guaranteed LOGOUT.

    IMAP login uses ICLOUD_IMAP_USER if set (the iCloud Mail mailbox
    address — usually @icloud.com), else falls back to ICLOUD_APPLE_ID.
    Apple's IMAP server rejects non-mailbox-identifier usernames even
    when the Apple ID + app-specific password are valid.
    """
    import ssl

    apple_id, app_pw = require_creds()
    imap_user = ICLOUD_IMAP_USER or apple_id
    client = imaplib.IMAP4_SSL(
        ICLOUD_IMAP_HOST, ICLOUD_IMAP_PORT, timeout=NETWORK_TIMEOUT
    )
    try:
        client.login(imap_user, app_pw)
        yield client
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError, ssl.SSLError):
            client.logout()
        with contextlib.suppress(Exception):
            client.shutdown()


_eventkit_store: Any | None = None


def eventkit_store():
    """Return a process-cached `EKEventStore` with reminders access requested.

    The first call triggers the TCC permission flow if not already granted.
    Subsequent calls reuse the same store object.
    """
    global _eventkit_store
    if _eventkit_store is not None:
        return _eventkit_store

    from EventKit import EKEventStore

    store = EKEventStore.alloc().init()

    _wait_for_reminders_access(store)

    _eventkit_store = store
    return store


def _wait_for_reminders_access(store) -> None:
    import threading

    done = threading.Event()
    granted: dict[str, Any] = {"ok": False, "err": None}

    def cb(ok: bool, err: Any) -> None:
        granted["ok"] = bool(ok)
        granted["err"] = err
        done.set()

    # macOS 14+: requestFullAccessToReminders... Older selectors remain for
    # compatibility. Calendar actions use CalDAV and need no EventKit access.
    if hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
        store.requestFullAccessToRemindersWithCompletion_(cb)
    else:
        from EventKit import EKEntityTypeReminder

        store.requestAccessToEntityType_completion_(EKEntityTypeReminder, cb)

    done.wait(timeout=10.0)
    if not granted["ok"]:
        raise RuntimeError(
            f"EventKit access denied for reminders: {granted['err']!r}. "
            f"Grant in System Settings → Privacy & Security → Reminders "
            f"for the binary at {_python_binary_path()}."
        )


def _python_binary_path() -> str:
    import sys
    return sys.executable


def reminders_access_granted() -> bool:
    """Whether macOS lets this process use Reminders right now. Never prompts.

    macOS decides by the process it holds responsible, not by the Python
    binary. A bridge started from the Claude desktop app is attributed to the
    desktop app, which cannot be granted Reminders access; the launchd
    background service is attributed to itself and holds the grant.
    """
    import sys

    if sys.platform != "darwin":
        return False
    try:
        from EventKit import EKAuthorizationStatusFullAccess, EKEntityTypeReminder, EKEventStore
    except ImportError:
        return False
    status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeReminder)
    return int(status) == EKAuthorizationStatusFullAccess


_carddav_addressbook_url: str | None = None


def carddav_http_client():
    """Return an httpx.Client configured with iCloud Basic-Auth.

    Caller is responsible for closing it (use as context manager). The auth
    is host-scoped — Authorization is dropped on any cross-origin redirect
    or on any request whose URL host isn't iCloud, so a poisoned `href`
    can't leak the app-specific password to a third-party host.
    """
    import httpx

    apple_id, app_pw = require_creds()
    return httpx.Client(
        auth=_HostScopedBasicAuth(apple_id, app_pw),
        timeout=NETWORK_TIMEOUT,
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": "brain-agents/0.5"},
        event_hooks={"response": [_reject_unsafe_redirect]},
    )


def carddav_addressbook_url(client=None) -> str:
    """Discover the user's default CardDAV address book URL, cached.

    iCloud's CardDAV path is sharded per-user (e.g.
    `https://p55-contacts.icloud.com/<user-id>/carddavhome/card/`).
    Discover once, cache for the process lifetime — the daemon restarts
    daily on launchd, so a sharding change is picked up at most one boot
    later. (No mid-process invalidation is implemented.)
    """
    global _carddav_addressbook_url
    if _carddav_addressbook_url:
        return _carddav_addressbook_url

    own_client = client is None
    if own_client:
        client = carddav_http_client()
    try:
        principal_url = assert_icloud_host(_discover_principal(client))
        home_url = assert_icloud_host(_discover_addressbook_home(client, principal_url))
        ab_url = assert_icloud_host(_discover_default_addressbook(client, home_url))
    finally:
        if own_client:
            client.close()

    _carddav_addressbook_url = ab_url
    return ab_url


def _propfind(client, url: str, body: str, depth: str = "0"):
    return client.request(
        "PROPFIND",
        url,
        content=body,
        headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
    )


def _discover_principal(client) -> str:
    import defusedxml.ElementTree as ET

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:">'
        '<d:prop><d:current-user-principal/></d:prop>'
        '</d:propfind>'
    )
    resp = _propfind(client, ICLOUD_CARDDAV_URL + "/", body)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    href = root.find(".//d:current-user-principal/d:href", CARDDAV_NS)
    if href is None or not href.text:
        raise RuntimeError("CardDAV: no current-user-principal in response")
    return _join(ICLOUD_CARDDAV_URL, href.text.strip())


def _discover_addressbook_home(client, principal_url: str) -> str:
    import defusedxml.ElementTree as ET

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        '<d:prop><c:addressbook-home-set/></d:prop>'
        '</d:propfind>'
    )
    resp = _propfind(client, principal_url, body)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    href = root.find(".//c:addressbook-home-set/d:href", CARDDAV_NS)
    if href is None or not href.text:
        raise RuntimeError("CardDAV: no addressbook-home-set")
    return _join(principal_url, href.text.strip())


def _discover_default_addressbook(client, home_url: str) -> str:
    import defusedxml.ElementTree as ET

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        '<d:prop>'
        '<d:resourcetype/>'
        '<d:displayname/>'
        '</d:prop>'
        '</d:propfind>'
    )
    resp = _propfind(client, home_url, body, depth="1")
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    for response in root.findall("d:response", CARDDAV_NS):
        rtype = response.find(".//d:resourcetype", CARDDAV_NS)
        if rtype is None:
            continue
        if rtype.find("c:addressbook", CARDDAV_NS) is None:
            continue
        href = response.find("d:href", CARDDAV_NS)
        if href is not None and href.text:
            return _join(home_url, href.text.strip())
    raise RuntimeError("CardDAV: no addressbook resource found under home")


def _join(base: str, path: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, path)
