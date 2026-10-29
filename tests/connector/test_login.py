from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.brain.connector.auth.login import login_routes
from src.brain.connector.auth.provider import VaultOAuthProvider, set_password, totp_setup
from src.brain.connector.auth.store import AuthStore
from src.brain.connector.auth.throttle import Throttle
from src.brain.connector.config import ConnectorConfig

PUBLIC_URL = "https://vault.example.com"
FULL = f"{PUBLIC_URL}/mcp"
LIMITED = f"{PUBLIC_URL}/mcp/limited"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "a long password for the vault connector"
OWNER_ADDR = "203.0.113.5"
STRANGER_ADDR = "198.51.100.7"
WRONG = "The password or the code is wrong."


class Clock:
    """The store's clock, moved by hand."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Ticker:
    """The throttle's clock in seconds, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


class Site:
    """A sign-in page with its own store, throttle and one registered client."""

    def __init__(
        self,
        folder: Path,
        *,
        password: bool = True,
        redirect_uri: str = CALLBACK,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.clock = Clock()
        self.ticker = Ticker()
        self.alerts: list[str] = []
        self.store = AuthStore(folder / "auth.db", clock=self.clock)
        config = ConnectorConfig(
            vault_path=folder / "vault",
            data_dir=folder,
            public_url=PUBLIC_URL,
            allowed_redirect_uris=(redirect_uri,),
        )
        self.provider = VaultOAuthProvider(config, self.store)
        self.throttle = Throttle(alert=alert or self.alerts.append, clock=self.ticker)
        self.totp: pyotp.TOTP | None = None
        if password:
            set_password(self.store, PASSWORD)
            self.totp = pyotp.TOTP(_query(totp_setup(self.store))["secret"])
        self.redirect_uri = redirect_uri
        self.client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="client-secret",
            redirect_uris=[AnyUrl(redirect_uri)],
            token_endpoint_auth_method="client_secret_post",
        )
        asyncio.run(self.provider.register_client(self.client))
        self.app = Starlette(routes=login_routes(self.provider, self.store, self.throttle))

    def start(self, resource: str = FULL) -> str:
        params = AuthorizationParams(
            state="state-1",
            scopes=None,
            code_challenge="c" * 43,
            redirect_uri=AnyUrl(self.redirect_uri),
            redirect_uri_provided_explicitly=True,
            resource=resource,
        )
        return _query(asyncio.run(self.provider.authorize(self.client, params)))["pending"]

    def browser(self, addr: str = OWNER_ADDR) -> TestClient:
        return TestClient(
            self.app, base_url=PUBLIC_URL, client=(addr, 50000), follow_redirects=False
        )

    def code(self, offset: int = 0) -> str:
        assert self.totp is not None
        return self.totp.at(self.clock.now, offset)

    def wrong_code(self) -> str:
        valid = {self.code(offset) for offset in (-1, 0, 1)}
        return next(code for code in ("000000", "111111", "222222", "333333") if code not in valid)

    def form(
        self, pending: str, password: str = PASSWORD, code: str | None = None
    ) -> dict[str, str]:
        return {
            "pending": pending,
            "password": password,
            "totp": self.code() if code is None else code,
        }


def test_login_shows_redirect_host(tmp_path: Path) -> None:
    site = Site(tmp_path)
    page = site.browser().get("/login", params={"pending": site.start()})
    assert page.status_code == 200
    assert "<strong>claude.ai</strong> is asking to use the full vault connector." in page.text
    limited = site.browser().get("/login", params={"pending": site.start(LIMITED)})
    assert (
        "<strong>claude.ai</strong> is asking to use the limited vault connector." in limited.text
    )

    odd = Site(tmp_path / "odd", redirect_uri='https://a&b"c.example/callback')
    escaped = odd.browser().get("/login", params={"pending": odd.start()})
    assert escaped.status_code == 200
    assert "<strong>a&amp;b&quot;c.example</strong>" in escaped.text
    assert 'a&b"c' not in escaped.text


def test_login_page_is_plain_and_not_cached(tmp_path: Path) -> None:
    site = Site(tmp_path)
    pending = site.start()
    page = site.browser().get("/login", params={"pending": pending})
    assert "<script" not in page.text.lower()
    assert f'<input type="hidden" name="pending" value="{pending}">' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


def test_login_wrong_totp_rejected(tmp_path: Path) -> None:
    site = Site(tmp_path)
    pending = site.start()
    browser = site.browser()

    wrong_code = browser.post("/login", data=site.form(pending, code=site.wrong_code()))
    assert wrong_code.status_code == 401
    assert "location" not in wrong_code.headers
    assert WRONG in wrong_code.text
    assert f'value="{pending}"' in wrong_code.text

    site.ticker.now += 60
    wrong_password = browser.post("/login", data=site.form(pending, password=PASSWORD + "!"))
    assert wrong_password.status_code == 401
    assert WRONG in wrong_password.text

    site.ticker.now += 60
    other_digits = site.code().translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    assert browser.post("/login", data=site.form(pending, code=other_digits)).status_code == 401

    site.ticker.now += 60
    assert browser.post("/login", data=site.form(pending)).status_code == 302


def test_login_accepts_codes_one_step_either_side(tmp_path: Path) -> None:
    site = Site(tmp_path)
    browser = site.browser()
    pending = site.start()
    assert browser.post("/login", data=site.form(pending, code=site.code(-2))).status_code == 401
    site.ticker.now += 1
    assert browser.post("/login", data=site.form(pending, code=site.code(2))).status_code == 401
    site.ticker.now += 2
    assert browser.post("/login", data=site.form(pending, code=site.code(-1))).status_code == 302
    spaced = site.code(1)[:3] + " " + site.code(1)[3:]
    assert browser.post("/login", data=site.form(site.start(), code=spaced)).status_code == 302


def test_login_success_redirects_with_code_and_state(tmp_path: Path) -> None:
    site = Site(tmp_path)
    pending = site.start(LIMITED)
    browser = site.browser()
    browser.post("/login", data=site.form(pending, code=site.wrong_code()))
    site.ticker.now += 1

    signed_in = browser.post("/login", data=site.form(pending))
    assert signed_in.status_code == 302
    location = signed_in.headers["location"]
    assert location.startswith(CALLBACK + "?")
    assert _query(location)["state"] == "state-1"
    code = asyncio.run(site.provider.load_authorization_code(site.client, _query(location)["code"]))
    assert code is not None and code.resource == LIMITED
    assert site.throttle.delay_for(OWNER_ADDR) == 0

    assert browser.get("/login", params={"pending": pending}).status_code == 400
    site.clock.now += timedelta(seconds=30)
    assert browser.post("/login", data=site.form(pending)).status_code == 400


def test_login_fails_until_password_and_code_are_set(tmp_path: Path) -> None:
    site = Site(tmp_path, password=False)
    pending = site.start()
    browser = site.browser()
    attempt = {"pending": pending, "password": PASSWORD, "totp": "123456"}

    refused = browser.post("/login", data=attempt)
    assert refused.status_code == 401
    assert "The connector has no password set yet." in refused.text

    set_password(site.store, PASSWORD)
    site.ticker.now += 60
    refused = browser.post("/login", data=attempt)
    assert refused.status_code == 401
    assert "The connector has no password set yet." in refused.text


def test_login_refuses_unknown_and_expired_requests(tmp_path: Path) -> None:
    site = Site(tmp_path)
    browser = site.browser()
    assert browser.get("/login").status_code == 400
    assert browser.get("/login", params={"pending": "unknown"}).status_code == 400

    sneaky = '"><b>sneaky</b>'
    refused = browser.post("/login", data=site.form(sneaky))
    assert refused.status_code == 400
    assert "sneaky" not in refused.text
    assert 'name="password"' not in refused.text

    pending = site.start()
    site.clock.now += timedelta(minutes=10)
    assert browser.get("/login", params={"pending": pending}).status_code == 400
    assert browser.post("/login", data=site.form(pending)).status_code == 400
    assert site.throttle.delay_for(OWNER_ADDR) == 0


def test_login_code_works_once(tmp_path: Path) -> None:
    site = Site(tmp_path)
    browser = site.browser()
    code = site.code()
    assert browser.post("/login", data=site.form(site.start(), code=code)).status_code == 302

    second = site.start()
    replayed = browser.post("/login", data=site.form(second, code=code))
    assert replayed.status_code == 401
    assert WRONG in replayed.text

    site.ticker.now += 1
    site.clock.now += timedelta(seconds=30)
    assert browser.post("/login", data=site.form(second, code=site.code(-1))).status_code == 401
    site.ticker.now += 2
    assert browser.post("/login", data=site.form(second)).status_code == 302


def test_login_refuses_large_forms(tmp_path: Path) -> None:
    site = Site(tmp_path)
    huge = site.browser().post("/login", data=site.form(site.start(), password="x" * 100_000))
    assert huge.status_code == 413
    assert site.throttle.delay_for(OWNER_ADDR) == 0


def test_throttle_doubles_and_caps_60(tmp_path: Path) -> None:
    ticker = Ticker()
    throttle = Throttle(alert=lambda message: None, clock=ticker)
    assert throttle.delay_for(STRANGER_ADDR) == 0
    delays = []
    for _ in range(9):
        throttle.failure(STRANGER_ADDR)
        delays.append(throttle.delay_for(STRANGER_ADDR))
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert throttle.retry_after(STRANGER_ADDR) == 60
    ticker.now += 59.5
    assert throttle.retry_after(STRANGER_ADDR) == 0.5
    ticker.now += 0.5
    assert throttle.retry_after(STRANGER_ADDR) == 0
    ticker.now += 3600
    assert throttle.delay_for(STRANGER_ADDR) == 0

    site = Site(tmp_path)
    pending = site.start()
    browser = site.browser()
    assert (
        browser.post("/login", data=site.form(pending, code=site.wrong_code())).status_code == 401
    )
    waiting = browser.post("/login", data=site.form(pending))
    assert waiting.status_code == 429
    assert "Try again in 1 second." in waiting.text
    assert waiting.headers["retry-after"] == "1"

    site.ticker.now += 1
    assert (
        browser.post("/login", data=site.form(pending, code=site.wrong_code())).status_code == 401
    )
    waiting = browser.post("/login", data=site.form(pending))
    assert waiting.status_code == 429
    assert "Try again in 2 seconds." in waiting.text

    site.ticker.now += 2
    assert browser.post("/login", data=site.form(pending)).status_code == 302


def test_no_global_lockout(tmp_path: Path) -> None:
    site = Site(tmp_path)
    stranger = site.browser(STRANGER_ADDR)
    pending = site.start()
    for _ in range(5):
        assert (
            stranger.post("/login", data=site.form(pending, code=site.wrong_code())).status_code
            == 401
        )
        site.ticker.now += 60
    assert (
        stranger.post("/login", data=site.form(pending, code=site.wrong_code())).status_code == 401
    )
    assert stranger.post("/login", data=site.form(pending)).status_code == 429

    assert site.throttle.delay_for(OWNER_ADDR) == 0
    owner = site.browser(OWNER_ADDR)
    assert owner.post("/login", data=site.form(site.start())).status_code == 302


def test_tries_from_one_address_run_one_at_a_time(tmp_path: Path) -> None:
    site = Site(tmp_path)
    wrong = site.form(site.start(), code=site.wrong_code())

    async def two_tries() -> list[int]:
        transport = httpx.ASGITransport(app=site.app, client=(STRANGER_ADDR, 50000))
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:
            responses = await asyncio.gather(
                http.post("/login", data=wrong), http.post("/login", data=wrong)
            )
        return sorted(response.status_code for response in responses)

    assert asyncio.run(two_tries()) == [401, 429]


def test_address_comes_from_a_swappable_function(tmp_path: Path) -> None:
    site = Site(tmp_path)
    app = Starlette(
        routes=login_routes(
            site.provider,
            site.store,
            site.throttle,
            address=lambda request: request.headers["x-client-address"],
        )
    )
    browser = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)
    pending = site.start()
    browser.post(
        "/login",
        data=site.form(pending, code=site.wrong_code()),
        headers={"x-client-address": STRANGER_ADDR},
    )
    assert site.throttle.delay_for(STRANGER_ADDR) == 1
    assert site.throttle.delay_for("testclient") == 0


def test_alert_after_10_failures_in_hour() -> None:
    ticker = Ticker()
    start = ticker.now
    alerts: list[str] = []
    throttle = Throttle(alert=alerts.append, clock=ticker)
    for count in range(1, 10):
        assert throttle.failure(STRANGER_ADDR) == count
        ticker.now += 60
    assert alerts == []
    assert throttle.failure(STRANGER_ADDR) == 10
    assert alerts == [f"Repeated failed sign-ins to the vault connector from {STRANGER_ADDR}"]
    assert throttle.failure(STRANGER_ADDR) == 11
    throttle.failure(OWNER_ADDR)
    assert len(alerts) == 1

    ticker.now = start + 3600 + 4 * 60
    assert [throttle.failure(STRANGER_ADDR) for _ in range(3)] == [7, 8, 9]
    assert len(alerts) == 1
    assert throttle.failure(STRANGER_ADDR) == 10
    assert len(alerts) == 2

    spread = Throttle(alert=alerts.append, clock=ticker)
    for _ in range(10):
        spread.failure(OWNER_ADDR)
        ticker.now += 7 * 60
    assert len(alerts) == 2


def test_alert_error_is_logged_and_counting_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    def broken(message: str) -> None:
        raise RuntimeError("reminders unavailable")

    throttle = Throttle(alert=broken, clock=Ticker())
    with caplog.at_level(logging.ERROR):
        counts = [throttle.failure(STRANGER_ADDR) for _ in range(11)]
    assert counts == list(range(1, 12))
    assert STRANGER_ADDR in caplog.text


def test_slow_alert_does_not_hold_up_other_sign_ins(tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()

    def slow_alert(message: str) -> None:
        started.set()
        release.wait(timeout=10)

    site = Site(tmp_path, alert=slow_alert)
    for _ in range(9):
        site.throttle.failure(STRANGER_ADDR)
    site.ticker.now += 60
    tenth_failure = site.form(site.start(), code=site.wrong_code())
    owner_sign_in = site.form(site.start())

    def browser(addr: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=site.app, client=(addr, 50000))
        return httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL)

    async def race() -> tuple[int, int, bool]:
        async with browser(STRANGER_ADDR) as stranger, browser(OWNER_ADDR) as owner:
            tenth = asyncio.create_task(stranger.post("/login", data=tenth_failure))
            try:
                assert await asyncio.to_thread(started.wait, 10)
                signed_in = await asyncio.wait_for(
                    owner.post("/login", data=owner_sign_in), timeout=3
                )
                alert_still_running = not release.is_set()
            finally:
                release.set()
            return (await tenth).status_code, signed_in.status_code, alert_still_running

    assert asyncio.run(race()) == (401, 302, True)


def test_ipv6_addresses_count_by_their_64_prefix(tmp_path: Path) -> None:
    alerts: list[str] = []
    throttle = Throttle(alert=alerts.append, clock=Ticker())
    throttle.failure("2001:db8:1:2::1")
    throttle.failure("2001:DB8:1:2:ffff:ffff:ffff:ffff")
    assert throttle.delay_for("2001:db8:1:2::abcd") == 2
    assert throttle.delay_for("2001:db8:1:3::1") == 0
    for n in range(3, 11):
        throttle.failure(f"2001:db8:1:2::{n:x}")
    assert alerts == ["Repeated failed sign-ins to the vault connector from 2001:db8:1:2::/64"]
    throttle.success("2001:db8:1:2::99")
    assert throttle.delay_for("2001:db8:1:2::1") == 0

    throttle.failure("::ffff:198.51.100.7")
    assert throttle.delay_for("198.51.100.7") == 1
    assert throttle.delay_for("::ffff:198.51.100.8") == 0

    site = Site(tmp_path)
    pending = site.start()
    wrong = site.form(pending, code=site.wrong_code())
    assert site.browser("2001:db8:5:6::1").post("/login", data=wrong).status_code == 401
    assert (
        site.browser("2001:db8:5:6::2").post("/login", data=site.form(pending)).status_code == 429
    )
    assert (
        site.browser("2001:db8:5:7::1").post("/login", data=site.form(pending)).status_code == 302
    )


def test_alert_when_password_right_but_code_wrong(tmp_path: Path) -> None:
    site = Site(tmp_path)
    pending = site.start()
    stranger = site.browser(STRANGER_ADDR)
    guess = site.form(pending, password="a guess", code=site.wrong_code())
    assert stranger.post("/login", data=guess).status_code == 401
    assert site.alerts == []

    site.ticker.now += 60
    assert (
        stranger.post("/login", data=site.form(pending, code=site.wrong_code())).status_code == 401
    )
    first = f"Correct password but wrong code for the vault connector from {STRANGER_ADDR}"
    assert site.alerts == [first]
    other = site.browser("192.0.2.9")
    assert other.post("/login", data=site.form(pending, code=site.wrong_code())).status_code == 401
    assert site.alerts == [first]

    site.ticker.now += 3600
    owner = site.browser()
    code = site.code()
    assert owner.post("/login", data=site.form(site.start(), code=code)).status_code == 302
    assert owner.post("/login", data=site.form(site.start(), code=code)).status_code == 401
    assert site.alerts == [first]
    assert other.post("/login", data=site.form(pending, code=site.wrong_code())).status_code == 401
    assert site.alerts == [
        first,
        "Correct password but wrong code for the vault connector from 192.0.2.9",
    ]


def test_alert_at_50_failures_from_all_addresses() -> None:
    ticker = Ticker()
    alerts: list[str] = []
    throttle = Throttle(alert=alerts.append, clock=ticker)
    message = "50 failed sign-ins to the vault connector in the last hour"
    for n in range(49):
        throttle.failure(f"192.0.2.{n}")
    assert alerts == []
    throttle.failure("192.0.2.200")
    assert alerts == [message]
    for n in range(10):
        throttle.failure(f"198.51.100.{n}")
    assert alerts == [message]

    ticker.now += 3600
    for n in range(49):
        throttle.failure(f"203.0.113.{n}")
    assert alerts == [message]
    throttle.failure("203.0.113.200")
    assert alerts == [message, message]
