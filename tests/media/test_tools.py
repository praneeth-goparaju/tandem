from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.brain.actions import queue
from src.brain.actions.approval_review import build_approval_review, payload_validation_error
from src.brain.actions.types import Action, ActionType
from src.brain.mcp.caller_context import ToolCaller, caller_scope
from src.brain.mcp.tools.media import caller_place, draw_chart, draw_image
from src.brain.mcp.tools.notifications import push_notify
from src.brain.mcp.tools.replies import reply_to_message
from src.brain.plugins.notifiers.slack import SlackNotifier
from src.db import get_media
from src.orchestrator.runners.brain_tool_bridge import ReplyDeliveryTracker

CHECKOUT = Path(__file__).resolve().parents[2]
GOOD = {"title": "Weight", "kind": "line", "x": ["Jan", "Feb"], "series": [{"name": "kg", "values": [83.1, 82.4]}]}
OK = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="teal"/></svg>'
ID = re.compile(r"m-[0-9a-f]{12}")

REGISTRY = {
    "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "DPRIV"}}},
    "family": {"chat": None, "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "health": {"chat": None, "delivery": {"slack": {"channel_id": "DPRIV"}}},
    "housekeeping": {"chat": None, "kind": "system", "delivery": None},
}


def text_of(result):
    return result["content"][0]["text"]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", REGISTRY)


@pytest.fixture
def private_caller(registry):
    with caller_scope(ToolCaller(agent="alfred_private", area="alfred_private")):
        yield


@pytest.fixture
def shared_caller(registry):
    with caller_scope(ToolCaller(agent="alfred", area="alfred", areas=frozenset({"alfred", "family"}))):
        yield


def test_draw_chart_saves_and_answers_with_an_id(isolated_db, tmp_path, monkeypatch, private_caller):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    answer = text_of(run(draw_chart({"spec": GOOD})))
    mid = ID.search(answer).group(0)
    row = get_media(mid)
    assert row["place"] == "private" and row["kind"] == "chart"
    assert re.fullmatch(
        rf"media id {mid}: chart 1080x1350 png, {row['bytes']} bytes\. Pass it in media_ids to post it\.", answer,
    )
    assert (row["agent"], row["area"]) == ("alfred_private", "alfred_private")
    assert row["run_label"].startswith("alfred_private@")
    assert (tmp_path / "media" / f"{mid}.png").stat().st_size == row["bytes"]


def test_draw_image_renders_saves_and_answers_with_an_id(isolated_db, tmp_path, monkeypatch, private_caller):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    answer = text_of(run(draw_image({"svg": OK})))
    mid = ID.search(answer).group(0)
    row = get_media(mid)
    assert row["kind"] == "image" and row["place"] == "private"
    assert answer == f"media id {mid}: image 200x100 png, {row['bytes']} bytes. Pass it in media_ids to post it."


def test_draw_image_refuses_a_script_without_writing(isolated_db, tmp_path, monkeypatch, private_caller):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    result = run(draw_image({"svg": OK.replace("<rect", "<script/><rect")}))
    assert result["is_error"] is True and text_of(result).startswith("ERROR") and "script" in text_of(result)
    assert not (tmp_path / "media").exists() or not list((tmp_path / "media").glob("*"))
    assert isolated_db.get_db().execute("SELECT COUNT(*) FROM media").fetchone() == (0,)


def test_draw_chart_refuses_a_bad_spec_with_the_reason_and_writes_nothing(
    isolated_db, tmp_path, monkeypatch, private_caller,
):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    result = run(draw_chart({"spec": {**GOOD, "series": [{"name": "kg", "values": [83.1]}]}}))
    assert result["is_error"] is True and "values" in text_of(result)
    assert isolated_db.get_db().execute("SELECT COUNT(*) FROM media").fetchone() == (0,)


@pytest.mark.parametrize("tool, args, message", [
    (draw_chart, {}, "spec must be an object"),
    (draw_chart, {"spec": "a chart"}, "spec must be an object"),
    (draw_chart, {"spec": [GOOD]}, "spec must be an object"),
    (draw_image, {}, "svg must be a string"),
    (draw_image, {"svg": {"a": 1}}, "svg must be a string"),
])
def test_the_tools_refuse_arguments_of_the_wrong_type(isolated_db, private_caller, tool, args, message):
    result = run(tool(args))
    assert result["is_error"] is True and message in text_of(result)


def test_shared_chat_media_carry_the_shared_place(isolated_db, tmp_path, monkeypatch, shared_caller):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    mid = ID.search(text_of(run(draw_chart({"spec": GOOD})))).group(0)
    assert get_media(mid)["place"] == "shared"


@pytest.mark.parametrize("caller, place", [
    (ToolCaller(agent="family", area="family"), "shared"),
    (ToolCaller(agent="health", area="health"), "private"),
    (ToolCaller(agent="housekeeping", area="housekeeping"), "private"),
    (ToolCaller(agent="x"), "private"),
    (ToolCaller(agent="x", areas=frozenset({"family"})), "shared"),
    (ToolCaller(agent="x", area="not-in-the-registry"), "private"),
    (ToolCaller(agent="x", area="not-in-the-registry", areas=frozenset({"family"})), "shared"),
])
def test_the_place_of_a_caller(registry, caller, place):
    assert caller_place(caller) == place


@pytest.mark.parametrize("tool, args", [(draw_chart, {"spec": GOOD}), (draw_image, {"svg": OK})])
def test_the_tools_need_a_caller(isolated_db, tool, args):
    result = run(tool(args))
    assert result == {"is_error": True, "content": [{"type": "text", "text": "ERROR: trusted caller context is required"}]}


def test_the_two_tools_are_registered_with_their_inputs():
    from src.brain.mcp.server import load_brain_tools

    tools = {spec.name: spec for spec in load_brain_tools()}
    assert tools["draw_chart"].input_schema["required"] == ["spec"]
    assert tools["draw_chart"].input_schema["properties"]["spec"]["type"] == "object"
    assert tools["draw_image"].input_schema["required"] == ["svg"]
    assert tools["draw_image"].input_schema["properties"]["svg"]["type"] == "string"
    description = tools["draw_image"].description
    assert "presentation attributes" in description and "<defs>" in description
    assert "url(" in description and "style" in description
    assert "media_ids" in tools["draw_chart"].description


def test_importing_the_tools_does_not_load_the_plotting_or_drawing_libraries():
    result = subprocess.run(
        [sys.executable, "-I", "-c", (
            f"import sys; sys.path.insert(0, {str(CHECKOUT)!r})\n"
            "import src.brain.mcp.tools.media\n"
            "print(sorted(m for m in ('matplotlib', 'cairosvg', 'src.brain.media.charts') if m in sys.modules))"
        )],
        capture_output=True, text=True, timeout=120, env={**os.environ, "USER_DIR": str(CHECKOUT / "tests/fixtures/user-ci")},
    )
    assert result.stdout.strip() == "[]", result.stderr


FIRST, SECOND = "m-0123456789ab", "m-ba9876543210"
FIVE = [f"m-{i:012x}" for i in range(5)]


def _fake_notifier(**result):
    notifier = MagicMock()
    notifier.send = AsyncMock(return_value={"status": "sent", "id": "123.456", "group_id": "C1", **result})
    notifier.persona_for = lambda *args, **kwargs: None
    return notifier


def _reply(args, notifier):
    caller = ToolCaller(agent="health", area="health", is_reply=True)
    with patch("src.brain.mcp._common.notifier", return_value=notifier), caller_scope(caller):
        return run(reply_to_message(args))


def _push(args, notifier):
    caller = ToolCaller(agent="health", area="health", allowed_action_types=frozenset({"push_notification"}))
    with patch("src.brain.actions.handlers.push_notification.notifier", return_value=notifier), caller_scope(caller):
        return run(push_notify(args))


def test_reply_passes_media_ids_to_the_notifier_and_keeps_its_answer(isolated_db):
    notifier = _fake_notifier()
    result = _reply({"text": "Here", "media_ids": [FIRST, SECOND, FIRST]}, notifier)
    assert text_of(result) == "reply:sent id=123.456"
    assert notifier.send.await_args.kwargs["media_ids"] == [FIRST, SECOND]


def test_a_reply_without_media_calls_the_notifier_exactly_as_before(isolated_db):
    notifier = _fake_notifier()
    _reply({"text": "Here"}, notifier)
    assert "media_ids" not in notifier.send.await_args.kwargs


def test_reply_names_the_media_the_notifier_did_not_post(isolated_db):
    notifier = _fake_notifier(refused_media=[FIRST], failed_media=[SECOND], media_errors={SECOND: "missing_scope"})
    result = _reply({"text": "Here", "media_ids": [FIRST, SECOND]}, notifier)
    lines = text_of(result).splitlines()
    assert lines[0] == "reply:sent id=123.456"
    assert lines[1].startswith(f"refused media: {FIRST}") and lines[2] == f"failed media: {SECOND} (missing_scope)"
    tracker = ReplyDeliveryTracker()
    tracker.observe("reply_to_message", result)
    assert tracker.delivered


@pytest.mark.parametrize("value, message", [
    (FIVE, "at most 4 media ids"),
    ("m-0123456789ab", "list"),
    (["nope"], "m-0123456789ab"),
])
def test_reply_refuses_bad_media_ids_before_sending(isolated_db, value, message):
    notifier = _fake_notifier()
    result = _reply({"text": "x", "media_ids": value}, notifier)
    assert result["is_error"] is True and message in text_of(result)
    notifier.send.assert_not_awaited()


def test_push_notify_carries_media_ids_through_the_queue_to_the_notifier(isolated_db):
    notifier = _fake_notifier()
    answer = text_of(_push({"title": "T", "message": "M", "media_ids": [FIRST, SECOND]}, notifier))
    assert "executed" in answer
    assert notifier.send.await_args.kwargs["media_ids"] == [FIRST, SECOND]
    assert json.loads(queue.list_by_status("executed")[0]["payload_json"])["media_ids"] == [FIRST, SECOND]


def test_a_push_without_media_adds_nothing_to_the_payload_or_the_call(isolated_db):
    notifier = _fake_notifier()
    _push({"title": "T", "message": "M"}, notifier)
    assert "media_ids" not in notifier.send.await_args.kwargs
    assert "media_ids" not in json.loads(queue.list_by_status("executed")[0]["payload_json"])


def test_push_notify_names_the_media_the_notifier_did_not_post(isolated_db):
    notifier = _fake_notifier(refused_media=[FIRST], failed_media=[SECOND], media_errors={SECOND: "not_in_channel"})
    answer = text_of(_push({"title": "T", "message": "M", "media_ids": [FIRST, SECOND]}, notifier))
    assert f"refused media: {FIRST}" in answer and f"failed media: {SECOND} (not_in_channel)" in answer


def test_push_notify_refuses_bad_media_ids_before_queueing(isolated_db):
    notifier = _fake_notifier()
    result = _push({"title": "T", "message": "M", "media_ids": FIVE}, notifier)
    assert result["is_error"] is True and "at most 4 media ids" in text_of(result)
    assert isolated_db.get_db().execute("SELECT COUNT(*) FROM pending_actions").fetchone() == (0,)
    notifier.send.assert_not_awaited()


@pytest.mark.parametrize("media_ids, valid", [
    ([FIRST], True),
    ([FIRST, SECOND], True),
    ([], True),
    (["nope"], False),
    ([FIRST, FIRST], False),
    (FIVE, False),
    (FIRST, False),
    ([7], False),
    (None, False),
])
def test_a_queued_push_may_carry_only_valid_media_ids(media_ids, valid):
    payload = {"title": "t", "message": "m", "media_ids": media_ids}
    assert (payload_validation_error(ActionType.PUSH_NOTIFICATION, payload) is None) is valid


def test_an_approval_review_of_a_push_lists_its_media():
    row = Action(
        id="abcdef123456", type=ActionType.PUSH_NOTIFICATION, rationale="weekly chart",
        payload={"title": "T", "message": "M", "media_ids": [FIRST, SECOND]},
    ).to_row()
    review = build_approval_review(row)
    assert review.approvable is True
    [line] = [line for line in review.summary_lines if line.startswith("Attached media:")]
    assert FIRST in line and SECOND in line


def test_the_push_handler_still_reports_media_when_the_detail_thread_failed(isolated_db):
    from src.brain.actions.handlers.push_notification import execute

    notifier = _fake_notifier(refused_media=[FIRST])
    notifier.send = AsyncMock(side_effect=[
        {"status": "sent", "id": "1.2", "group_id": "C1", "refused_media": [FIRST]},
        {"status": "failed", "id": None, "error": "boom"},
    ])
    with patch("src.brain.actions.handlers.push_notification.notifier", return_value=notifier):
        result = run(execute({
            "title": "T", "message": "M", "thread_detail": "more", "media_ids": [FIRST],
            "_origin_agent": "health", "_origin_area": "health",
        }))
    assert "detail not delivered" in result["result"] and f"refused media: {FIRST}" in result["result"]
    assert "media_ids" not in notifier.send.await_args_list[1].kwargs


def test_a_push_whose_upload_failed_is_a_partial_success_naming_the_failed_id(isolated_db):
    from src.brain.actions.handlers.push_notification import execute

    notifier = _fake_notifier(failed_media=[SECOND], media_errors={SECOND: "missing_scope"})
    with patch("src.brain.actions.handlers.push_notification.notifier", return_value=notifier):
        result = run(execute({
            "title": "T", "message": "M", "media_ids": [FIRST, SECOND],
            "_origin_agent": "health", "_origin_area": "health",
        }))
    assert result["ok"] is True and result["partial"] is True
    assert result["result"].splitlines() == ["notifier:sent ts=123.456", f"failed media: {SECOND} (missing_scope)"]


def test_an_approved_push_whose_upload_failed_is_not_marked_fully_delivered(isolated_db):
    from src.brain.actions import executor
    from src.brain.actions.types import Action, Risk

    queue.insert(Action(
        id="push-media", type=ActionType.PUSH_NOTIFICATION, rationale="weekly chart", risk=Risk.CONFIRM,
        payload={"title": "T", "message": "M", "media_ids": [FIRST]},
        origin_agent="health", origin_area="health",
    ))
    queue.transition_status("push-media", "pending", "approved")
    notifier = _fake_notifier(failed_media=[FIRST], media_errors={FIRST: "missing_scope"})
    with patch("src.brain.actions.handlers.push_notification.notifier", return_value=notifier):
        run(executor._run_one(queue.get("push-media")))
    row = queue.get("push-media")
    assert row["status"] == "partial"
    assert f"failed media: {FIRST} (missing_scope)" in row["result"]


def test_a_push_whose_media_were_only_refused_stays_a_full_success(isolated_db):
    from src.brain.actions.handlers.push_notification import execute

    with patch("src.brain.actions.handlers.push_notification.notifier",
               return_value=_fake_notifier(refused_media=[FIRST])):
        result = run(execute({"title": "T", "message": "M", "media_ids": [FIRST],
                              "_origin_agent": "health", "_origin_area": "health"}))
    assert result["ok"] is True and "partial" not in result


def test_the_posting_tools_take_at_most_four_media_ids():
    from src.brain.mcp.server import load_brain_tools

    tools = {spec.name: spec for spec in load_brain_tools()}
    for schema in (
        tools["reply_to_message"].input_schema["properties"]["media_ids"],
        tools["push_notify"].input_schema["properties"]["media_ids"],
    ):
        assert schema["type"] == "array" and schema["maxItems"] == 4 and schema["uniqueItems"] is True
        assert schema["items"] == {"type": "string", "pattern": "^m-[0-9a-f]{12}$"}


def test_a_drawn_chart_is_posted_in_its_own_place_and_refused_in_the_other(isolated_db, tmp_path, monkeypatch, registry):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    client.files_upload_v2 = AsyncMock(return_value={"ok": True})
    notifier = SlackNotifier(bot_token="xoxb-test", channels={"default": {"channel_id": "C_DEFAULT"}})
    notifier._client = client

    def as_reply_from(area, args, tool):
        with patch("src.brain.mcp._common.notifier", return_value=notifier), caller_scope(
            ToolCaller(agent=area, area=area, is_reply=True),
        ):
            return text_of(run(tool(args)))

    mid = ID.search(as_reply_from("health", {"spec": GOOD}, draw_chart)).group(0)
    posted = as_reply_from("health", {"text": "Your chart", "media_ids": [mid]}, reply_to_message)
    refused = as_reply_from("family", {"text": "Your chart", "media_ids": [mid]}, reply_to_message)

    assert posted == "reply:sent id=9.9"
    assert client.files_upload_v2.await_count == 1
    assert client.files_upload_v2.await_args.kwargs["channel"] == "DPRIV"
    assert client.files_upload_v2.await_args.kwargs["file"] == str(tmp_path / "media" / f"{mid}.png")
    assert refused.splitlines()[0] == "reply:sent id=9.9" and f"refused media: {mid}" in refused
    assert get_media(mid)["sent_channel"] == "DPRIV"
