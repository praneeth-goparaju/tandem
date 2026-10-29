from __future__ import annotations

from src.brain.actions.outcomes import (
    ambiguous_failure,
    failure_from_exception,
    is_retryable_http_status,
)


def test_ambiguous_failure_is_terminal_and_explicit() -> None:
    assert ambiguous_failure("unknown") == {
        "ok": False,
        "result": "unknown",
        "ambiguous": True,
    }


def test_exception_message_does_not_make_logic_error_retryable() -> None:
    outcome = failure_from_exception(
        "handler failed",
        RuntimeError("validation mentions connection timeout"),
    )

    assert outcome["ok"] is False
    assert outcome.get("retryable") is not True


def test_typed_timeout_is_retryable() -> None:
    outcome = failure_from_exception("handler failed", TimeoutError("slow"))

    assert outcome["retryable"] is True


def test_http_retry_statuses_are_narrow() -> None:
    assert is_retryable_http_status(429)
    assert is_retryable_http_status(503)
    assert not is_retryable_http_status(400)
    assert not is_retryable_http_status(401)
