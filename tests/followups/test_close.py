import asyncio
import json

import pytest

from src.brain.actions import queue
from src.brain.actions.types import Action, ActionType, Risk
from src.brain.followups.close import close_follow_up, linked_reminder_ids
from src.db import get_follow_up, set_follow_up_work, update_follow_up, upsert_follow_up


@pytest.fixture
def ran(isolated_db, monkeypatch):
    done = []

    async def fake_run_one(row):
        done.append(row)
        queue.transition_status(row["id"], "approved", "executed", result="ok")

    monkeypatch.setattr("src.brain.actions.executor._run_one", fake_run_one)
    return done


def item(status="raised"):
    follow_up_id = upsert_follow_up(source="alfred", ref="alfred:1", asked_by="owner", place="private",
                                    summary="Send Andrei the CV", dedupe_key="k1", first_seen="2026-10-16T05:00:00")
    update_follow_up(follow_up_id, status=status)
    return follow_up_id


def reminder(action_id="abcdef123456", result="created reminder id=REM-42"):
    queue.insert(Action(id=action_id, type=ActionType.CREATE_REMINDER, risk=Risk.AUTO, rationale="r",
                        payload={"title": "Send the CV", "due": "2026-10-18T09:00:00+02:00"},
                        origin_agent="follow_up", origin_area="follow_up"))
    queue.transition_status(action_id, "pending", "approved")
    queue.transition_status(action_id, "approved", "executed", result=result)
    return action_id


def test_done_closes_the_item_and_ticks_its_reminder(ran):
    follow_up_id = item()
    set_follow_up_work(follow_up_id, outcome="prepared", action_ids=[reminder()], night_of="2026-10-16")

    assert asyncio.run(close_follow_up(follow_up_id, "done", actor="owner")) is True

    assert get_follow_up(follow_up_id)["status"] == "done"
    [tick] = [row for row in ran if row["type"] == "complete_reminder"]
    assert json.loads(tick["payload_json"]) == {"id": "REM-42"} and tick["risk"] == "auto"


def test_drop_dismisses_and_also_ticks(ran):
    follow_up_id = item()
    set_follow_up_work(follow_up_id, outcome="prepared", action_ids=[reminder()], night_of="2026-10-16")
    assert asyncio.run(close_follow_up(follow_up_id, "dismissed", actor="owner")) is True
    assert get_follow_up(follow_up_id)["dismiss_count"] == 1
    assert [row["type"] for row in ran] == ["complete_reminder"]


def test_a_closed_item_changes_nothing_and_ticks_nothing(ran):
    follow_up_id = item(status="done")
    set_follow_up_work(follow_up_id, outcome="prepared", action_ids=[reminder()], night_of="2026-10-16")
    assert asyncio.run(close_follow_up(follow_up_id, "done", actor="owner")) is False
    assert ran == []


def test_only_executed_reminders_of_the_item_count(isolated_db):
    good = reminder("aaaaaaaaaaaa", "created reminder id=REM-1")
    queue.insert(Action(id="bbbbbbbbbbbb", type=ActionType.PUSH_NOTIFICATION, risk=Risk.AUTO, rationale="r",
                        payload={"title": "t", "message": "m"}, origin_agent="x", origin_area="x"))
    assert linked_reminder_ids({"action_ids": json.dumps([good, "bbbbbbbbbbbb", "cccccccccccc"])}) == ["REM-1"]
