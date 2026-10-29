"""The vault connector's sign-in page: plain HTML from two Starlette routes.

``GET /login?pending=<id>`` names the site the code will be sent back to (the
host of the sign-in's redirect address, from the server's own records) and the
endpoint asked for, and shows a form for the password and the six-digit code.

``POST /login`` handles one try at a time. It first checks whether the
client's address must still wait after earlier failures (429, nothing else is
looked at), then the sign-in request (400 when it is unknown, expired or
used), then the password and the code. On success the browser goes back to
the redirect address with the code; on failure the failure is recorded for
the address and the form comes back with a plain message and status 401.
Alerts the failure makes due are sent after the response has gone out.
Nothing ever sleeps.

The page has no JavaScript and cannot be framed or cached. The client's
address comes from ``address``, which by default is the peer address the
server saw.
"""

from __future__ import annotations

import asyncio
import html
import math
from collections.abc import Callable

import anyio.to_thread
from mcp.server.transport_security import RequestBodyLimitMiddleware
from starlette.background import BackgroundTask
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

from src.brain.connector.auth.provider import (
    SignInRequest,
    VaultOAuthProvider,
    check_password_and_code,
    sign_in_set_up,
)
from src.brain.connector.auth.store import AuthStore
from src.brain.connector.auth.throttle import Throttle

_MAX_FORM_BYTES = 16 * 1024
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
_STYLE = """
body { font-family: -apple-system, system-ui, sans-serif; max-width: 26rem; margin: 3rem auto;
       padding: 0 1rem; line-height: 1.5; }
label { display: block; margin-top: 1rem; }
input { box-sizing: border-box; width: 100%; padding: 0.5rem; font-size: 1rem; }
button { margin-top: 1.5rem; padding: 0.5rem 1.5rem; font-size: 1rem; }
.message { color: #b00020; }
"""
_EXPIRED = "This sign-in link is no longer valid. Start again from Claude."
_NOT_SET_UP = "The connector has no password set yet."
_WRONG = (
    "The password or the code is wrong. Each code works only once, so if you just used it, "
    "wait for the next one."
)


def request_address(request: Request) -> str:
    """The address the request came from, as the server saw it."""
    return request.client.host if request.client else "unknown"


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _page(
    body: str,
    status: int,
    headers: dict[str, str] | None = None,
    background: BackgroundTask | None = None,
) -> HTMLResponse:
    document = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Sign in to Vault</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"<h1>Sign in to Vault</h1>\n{body}</body>\n</html>\n"
    )
    return HTMLResponse(
        document,
        status_code=status,
        headers={**_HEADERS, **(headers or {})},
        background=background,
    )


def _message(text: str) -> str:
    return f'<p class="message">{html.escape(text)}</p>\n' if text else ""


def _form(
    sign_in: SignInRequest,
    pending_id: str,
    message: str,
    status: int,
    headers: dict[str, str] | None = None,
    background: BackgroundTask | None = None,
) -> HTMLResponse:
    return _page(
        f"<p><strong>{html.escape(sign_in.host)}</strong> is asking to use the "
        f"{sign_in.access} vault connector.</p>\n"
        "<p>Sign in only if you just added or reconnected the connector in Claude.</p>\n"
        f"{_message(message)}"
        '<form method="post">\n'
        f'<input type="hidden" name="pending" value="{html.escape(pending_id)}">\n'
        '<label for="password">Password</label>\n'
        '<input id="password" name="password" type="password" autocomplete="current-password" '
        "required autofocus>\n"
        '<label for="totp">Six-digit code</label>\n'
        '<input id="totp" name="totp" inputmode="numeric" autocomplete="one-time-code" required>\n'
        '<button type="submit">Sign in</button>\n'
        "</form>\n",
        status,
        headers,
        background,
    )


def _sending(throttle: Throttle, alerts: list[str]) -> BackgroundTask | None:
    return BackgroundTask(throttle.send, alerts) if alerts else None


def login_routes(
    provider: VaultOAuthProvider,
    store: AuthStore,
    throttle: Throttle,
    address: Callable[[Request], str] = request_address,
) -> list[Route]:
    """The GET and POST routes of ``/login``.

    POST tries are checked one at a time across all addresses, first come
    first served (one global ``asyncio.Lock``). That keeps parallel tries from
    one address from slipping past its wait, and caps how fast anyone can
    guess. Nothing slow but the password check runs under the lock: alerts
    are sent after it is released.
    """
    one_at_a_time = asyncio.Lock()

    async def show(request: Request) -> Response:
        pending_id = request.query_params.get("pending", "")
        sign_in = provider.sign_in_request(pending_id)
        if sign_in is None:
            return _page(_message(_EXPIRED), 400)
        return _form(sign_in, pending_id, "", 200)

    async def submit(request: Request) -> Response:
        form = await request.form()
        pending_id = _text(form, "pending")
        client = address(request)
        async with one_at_a_time:
            wait = math.ceil(throttle.retry_after(client))
            sign_in = provider.sign_in_request(pending_id)
            if wait > 0:
                unit = "second" if wait == 1 else "seconds"
                text = f"Too many failed sign-ins from your address. Try again in {wait} {unit}."
                retry = {"Retry-After": str(wait)}
                if sign_in is None:
                    return _page(_message(text), 429, retry)
                return _form(sign_in, pending_id, text, 429, retry)
            if sign_in is None:
                return _page(_message(_EXPIRED), 400)
            if not sign_in_set_up(store):
                _, alerts = throttle.record_failure(client)
                return _form(
                    sign_in, pending_id, _NOT_SET_UP, 401, background=_sending(throttle, alerts)
                )
            password, code = _text(form, "password"), _text(form, "totp")
            result = await anyio.to_thread.run_sync(check_password_and_code, store, password, code)
            if result != "right":
                _, alerts = throttle.record_failure(client, only_code_wrong=result == "wrong code")
                return _form(
                    sign_in, pending_id, _WRONG, 401, background=_sending(throttle, alerts)
                )
            throttle.success(client)
            target = provider.issue_code(pending_id)
        if target is None:
            return _page(_message(_EXPIRED), 400)
        return RedirectResponse(target, status_code=302, headers=_HEADERS)

    return [
        Route("/login", show, methods=["GET"]),
        Route(
            "/login",
            RequestBodyLimitMiddleware(request_response(submit), _MAX_FORM_BYTES),
            methods=["POST"],
        ),
    ]
