from __future__ import annotations

import os


def test_suite_never_holds_a_real_slack_token() -> None:
    from src.brain.mcp import _common

    assert os.environ.get("SLACK_BOT_TOKEN", "") == ""

    _common.notifier.cache_clear()
    live = _common.notifier()
    assert not getattr(live, "bot_token", ""), "the notifier was built with a real token"
