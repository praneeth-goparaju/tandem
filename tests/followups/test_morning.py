import asyncio
from datetime import date

import pytest

from src.brain.followups import morning
from src.db import get_db, set_follow_up_work, update_follow_up, upsert_follow_up

TODAY = date(2026, 10, 17)
NIGHT = "2026-10-16"
REGISTRY = {
    "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "DPRIV"}}},
}


@pytest.fixture
def env(isolated_db):
    return isolated_db


def worked(n: int, *, status: str = "raised", outcome: str = "asked", night_of: str = NIGHT) -> str:
    item = upsert_follow_up(
        source="slack_private", ref=f"ref-{n}", asked_by="owner", place="private",
        summary=f"Loop {n}", dedupe_key=f"key-{n}", first_seen=f"2026-10-1{n % 6}T05:00:00",
    )
    update_follow_up(item, status=status)
    set_follow_up_work(item, outcome=outcome, action_ids=None, night_of=night_of)
    return item


def needs(today: date = TODAY) -> list[str]:
    return [row["id"] for row in morning.needs_and_handled(date(2026, 10, 16), today)[0]]


def test_raised_items_need_the_owner_and_closed_ones_count_as_handled(env):
    first, second = worked(1), worked(2, outcome="found")
    done = worked(3, status="done", outcome="done")
    raised, handled = morning.needs_and_handled(date(2026, 10, 16), TODAY)
    assert [row["id"] for row in raised] == [first, second]
    assert [row["id"] for row in handled] == [done]
    assert morning.latest_night(TODAY) == date(2026, 10, 16)


def test_an_item_still_raised_from_an_earlier_night_is_shown(env):
    item = worked(1, night_of="2026-10-14")
    assert needs() == [item]


def test_a_raised_item_without_an_outcome_is_shown(env):
    item = upsert_follow_up(source="slack_private", ref="r9", asked_by="owner", place="private",
                            summary="No outcome", dedupe_key="k9", first_seen="2026-10-15T05:00:00")
    update_follow_up(item, status="raised")
    assert needs() == [item]


def test_an_open_item_that_was_never_raised_is_not_shown(env):
    worked(1, status="open")
    assert needs() == []


def test_snoozed_items_wait_until_their_day(env):
    item = worked(1)
    get_db().execute("UPDATE follow_ups SET last_raised='2026-10-18' WHERE id=?", (item,))
    get_db().commit()
    assert needs() == []
    assert needs(date(2026, 10, 18)) == [item]


def test_runner_refreshes_home_after_the_closing_run_and_the_night_shift(env, monkeypatch):
    from src.orchestrator import runner

    calls: list[str] = []

    async def fake_agent(name, runtime_ctx=None):
        calls.append(name)
        return {"agent": name, "status": "success"}

    async def fake_refresh():
        calls.append("home")

    monkeypatch.setattr(runner, "run_single_agent", fake_agent)
    monkeypatch.setattr(runner, "AGENT_REGISTRY", {**REGISTRY, "follow_up": {"chat": None}})
    monkeypatch.setattr(runner, "RUN_MODES", {
        "closing_solo": {"parallel": [], "sequential": ["alfred_private"]},
        "night": {"parallel": [], "sequential": ["follow_up"]},
    })
    monkeypatch.setattr(runner, "_refresh_home", fake_refresh)
    asyncio.run(runner.run_orchestrated("closing_solo"))
    assert calls == ["alfred_private", "home"]
    calls.clear()
    asyncio.run(runner.run_orchestrated("night"))
    assert calls == ["follow_up", "home"]
