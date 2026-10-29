from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.brain.runtime.template import build_context, build_env, render_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
BODY = '{% set your_area = "Finance" %}{% include "_area_owner.md" %}'


def _render(profile: dict) -> str:
    env = build_env(REPO_ROOT / "config")
    ctx = build_context(profile, {}, at=datetime(2026, 3, 2, 7, 0))
    return render_agent(env, BODY, ctx)


def test_no_register_note_renders_no_register_sentence() -> None:
    for profile in ({"user": {"name": "Alex"}}, {"user": {"name": "Alex"}, "notes": {}}):
        text = _render(profile)
        assert "register note" not in text
        assert "questions tagged to" not in text


def test_an_open_question_goes_on_the_to_do_list() -> None:
    text = _render({"user": {"name": "Alex"}})
    assert "follow_ups_open" in text
    assert "open item in `Actions/`" not in text


def test_the_named_register_note_sentence_is_the_old_one() -> None:
    text = _render({"user": {"name": "Alex"}, "notes": {"register_note": "Open Decisions.md"}})
    assert "Check `Open Decisions.md` for open questions tagged to Finance. If there's a live decision" in text
    assert "follow_ups_open" not in text
