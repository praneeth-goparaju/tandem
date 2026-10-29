from __future__ import annotations

from src.brain.doctor import issue_level

_MAX_LINES = 15


def format_failure_summary(results, *, limit: int = _MAX_LINES) -> str | None:
    """Render failing checks as plain lines, or None when nothing needs attention.

    `results` is run_checks' output: list[(Check, list[Issue])].
    """
    lines: list[str] = []
    for check, issues in results:
        for issue in issues:
            level = issue_level(check, issue)
            if level == "info":
                continue
            loc = f"{issue.file}:{issue.line}" if issue.line else issue.file
            lines.append(f"- {check.name} [{level}] {loc}: {issue.message}")
    if not lines:
        return None
    shown = lines[:limit]
    if len(lines) > len(shown):
        shown.append(f"...and {len(lines) - len(shown)} more issue(s)")
    return "\n".join(shown)


async def send_failure_ping(results, notifier=None) -> bool:
    """Post the failure summary to BRIEF_CHANNEL_ID. Returns True iff sent.

    No-ops (False) when the channel is unset or there is nothing to report.
    `notifier` is injectable for tests; the default builds a SlackNotifier
    pinned to the brief channel (never an area fallback — a doctor ping on
    the wrong channel is worse than none).
    """
    from src.orchestrator import config

    channel = getattr(config, "BRIEF_CHANNEL_ID", "")
    if not channel:
        return False
    text = format_failure_summary(results)
    if text is None:
        return False

    if notifier is None:
        from src.brain.plugins.notifiers.slack import SlackNotifier

        notifier = SlackNotifier(default_channel_id=channel)

    try:
        resp = await notifier.send(text, title="tandem doctor: failing checks")
    except Exception:  # noqa: BLE001
        return False
    return bool(resp) and resp.get("status") == "sent"
