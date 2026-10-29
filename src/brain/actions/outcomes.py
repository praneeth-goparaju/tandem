from __future__ import annotations

import asyncio
import imaplib
from typing import Any, NotRequired, Required, TypedDict


class HandlerResult(TypedDict, total=False):
    ok: Required[bool]
    result: Required[str]
    retryable: NotRequired[bool]
    partial: NotRequired[bool]
    ambiguous: NotRequired[bool]


class TransientActionError(RuntimeError):
    ...


def _as_dict(outcome: HandlerResult) -> dict[str, Any]:
    return dict(outcome)


def success(result: str) -> dict[str, Any]:
    return _as_dict({"ok": True, "result": result})


def partial_success(result: str) -> dict[str, Any]:
    """A terminal success where at least one optional side effect failed.

    The executor records this as executed, not failed: replaying the entire
    handler could duplicate the side effects that already succeeded.
    """
    return _as_dict({"ok": True, "result": result, "partial": True})


def ambiguous_failure(result: str) -> dict[str, Any]:
    """A terminal failure whose external effect may already have happened.

    The executor records this separately and never replays it automatically;
    an operator must reconcile the provider state first.
    """
    return _as_dict({"ok": False, "result": result, "ambiguous": True})


def permanent_failure(result: str) -> dict[str, Any]:
    return _as_dict({"ok": False, "result": result})


def retryable_failure(result: str) -> dict[str, Any]:
    return _as_dict({"ok": False, "result": result, "retryable": True})


def is_retryable_http_status(status: int | None) -> bool:
    return status in {408, 425, 429} or (status is not None and 500 <= status <= 599)


def is_transient_exception(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            TransientActionError,
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
            ConnectionError,
            imaplib.IMAP4.abort,
        ),
    ):
        return True

    try:
        import httpx

        if isinstance(exc, httpx.TransportError):
            return True
    except ImportError:
        pass

    try:
        import aiohttp

        if isinstance(
            exc,
            (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError),
        ):
            return True
    except ImportError:
        pass

    try:
        import requests

        if isinstance(
            exc,
            (requests.exceptions.ConnectionError, requests.exceptions.Timeout),
        ):
            return True
    except ImportError:
        pass

    response: Any = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status_code", None)
    return is_retryable_http_status(status if isinstance(status, int) else None)


def failure_from_exception(prefix: str, exc: BaseException) -> dict[str, Any]:
    detail = f"{prefix}: {type(exc).__name__}: {exc}"
    if is_transient_exception(exc):
        return retryable_failure(detail)
    return permanent_failure(detail)
