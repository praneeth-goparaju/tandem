from __future__ import annotations

import asyncio

from src.brain.doctor import Check, Issue, issue_level
from src.brain.doctor.notify import format_failure_summary, send_failure_ping


def _check(severity: str = "error") -> Check:
    return Check(name="check_example", severity=severity, fn=lambda cfg, root: [])  # type: ignore[arg-type]


def test_info_only_results_produce_no_summary_and_no_ping(monkeypatch) -> None:
    monkeypatch.setattr("src.orchestrator.config.BRIEF_CHANNEL_ID", "C123", raising=False)
    results = [(_check("warn"), [Issue("f", "integrations on: none", severity="info")])]
    assert format_failure_summary(results) is None

    class Notifier:
        async def send(self, *args, **kwargs):
            raise AssertionError("an info line must not alert")

    assert asyncio.run(send_failure_ping(results, notifier=Notifier())) is False


def test_a_warning_still_appears_labelled_warn() -> None:
    results = [
        (_check("warn"), [Issue("f", "fine", severity="info"), Issue("g", "careful")]),
    ]
    text = format_failure_summary(results)
    assert text is not None
    assert "[warn]" in text and "careful" in text and "fine" not in text


def test_the_label_follows_the_issue_not_the_check() -> None:
    results = [(_check("error"), [Issue("f", "careful", severity="warn")])]
    text = format_failure_summary(results)
    assert text is not None and "[warn]" in text
