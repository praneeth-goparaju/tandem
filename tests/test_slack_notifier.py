from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.brain.plugins.notifiers.slack import (
    APPROVE_ACTION_ID,
    REJECT_ACTION_ID,
    SlackNotifier,
    build_blocks,
    to_mrkdwn,
)


def test_mrkdwn_bold() -> None:
    assert to_mrkdwn("hello **world**") == "hello *world*"


def test_mrkdwn_italic_not_bold() -> None:
    assert to_mrkdwn("say *hi* there") == "say _hi_ there"


def test_mrkdwn_bold_and_italic_together() -> None:
    assert to_mrkdwn("**bold** and *italic*") == "*bold* and _italic_"


def test_mrkdwn_headings_become_bold() -> None:
    assert to_mrkdwn("# Heading\n\nbody") == "*Heading*\n\nbody"
    assert to_mrkdwn("## Sub") == "*Sub*"


def test_mrkdwn_leaves_strike_and_code_alone() -> None:
    s = "`snippet` and ~struck~ text"
    assert to_mrkdwn(s) == s


def test_mrkdwn_neutralizes_slack_control_mentions_but_keeps_links() -> None:
    value = "<!channel> <@U123> <#C123> [source](https://example.test)"
    assert to_mrkdwn(value) == (
        "&lt;!channel> &lt;@U123> &lt;#C123> <https://example.test|source>"
    )


def test_mrkdwn_blocks_control_mentions_created_by_markdown_links() -> None:
    rendered = to_mrkdwn(
        "[all](!channel) [user](@U123) [room](#C123) "
        "[source](https://example.test)"
    )

    assert "<!" not in rendered
    assert "<@" not in rendered
    assert "<#" not in rendered
    assert "<https://example.test|source>" in rendered


def test_mrkdwn_removes_invisible_unicode_controls() -> None:
    family = "👨‍👩‍👧‍👦"
    assert to_mrkdwn(
        f"{family} safe\x1b\u0085\u202e\u2028\ud800after"
    ) == f"{family} safeafter"


def test_build_blocks_no_table_returns_text_only() -> None:
    text, blocks = build_blocks("hello world")
    assert text == "hello world"
    assert blocks is None


def test_build_blocks_with_table_returns_blocks() -> None:
    md = (
        "Here are the listings:\n"
        "\n"
        "| price | rooms | sqm |\n"
        "|-------|-------|-----|\n"
        "| 450k  | 4     | 120 |\n"
        "| 520k  | 3     | 95  |\n"
    )
    text, blocks = build_blocks(md)
    assert blocks is not None
    assert blocks[0]["type"] == "section"
    assert "Here are the listings" in blocks[0]["text"]["text"]
    table = blocks[1]
    assert table["type"] == "table"
    assert table["rows"][0] == [
        {"type": "raw_text", "text": "price"},
        {"type": "raw_text", "text": "rooms"},
        {"type": "raw_text", "text": "sqm"},
    ]
    assert table["rows"][1] == [
        {"type": "raw_text", "text": "450k"},
        {"type": "raw_text", "text": "4"},
        {"type": "raw_text", "text": "120"},
    ]
    assert table["rows"][2] == [
        {"type": "raw_text", "text": "520k"},
        {"type": "raw_text", "text": "3"},
        {"type": "raw_text", "text": "95"},
    ]
    assert "price" in text


def test_build_blocks_text_after_table_becomes_section() -> None:
    md = (
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\n"
        "Trailing note here.\n"
    )
    text, blocks = build_blocks(md)
    assert blocks is not None
    types = [b["type"] for b in blocks]
    assert types == ["table", "section"]
    assert "Trailing note here" in blocks[-1]["text"]["text"]


def test_build_blocks_multiple_tables_first_as_block_rest_as_bullets() -> None:
    md = (
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\n"
        "next:\n"
        "| x | y |\n"
        "|---|---|\n"
        "| 9 | 8 |\n"
    )
    text, blocks = build_blocks(md)
    assert blocks is not None
    table_blocks = [b for b in blocks if b["type"] == "table"]
    assert len(table_blocks) == 1
    assert table_blocks[0]["rows"][0][0]["text"] == "a"
    lists = [
        el
        for b in blocks
        if b["type"] == "rich_text"
        for el in b["elements"]
        if el["type"] == "rich_text_list"
    ]
    assert [item["elements"] for lst in lists for item in lst["elements"]] == [
        [{"type": "text", "text": "x: 9 · y: 8"}]
    ]


def test_build_blocks_table_without_header_pipes_still_parses() -> None:
    md = "a | b\n---|---\n1 | 2\n"
    text, blocks = build_blocks(md)
    assert blocks is not None
    table = next(b for b in blocks if b["type"] == "table")
    assert table["rows"][0] == [
        {"type": "raw_text", "text": "a"},
        {"type": "raw_text", "text": "b"},
    ]
    assert table["rows"][1] == [
        {"type": "raw_text", "text": "1"},
        {"type": "raw_text", "text": "2"},
    ]


def test_build_blocks_table_cells_are_trimmed() -> None:
    md = "| col1 | col2 |\n|------|------|\n|   a  |  b   |\n"
    text, blocks = build_blocks(md)
    table = next(b for b in blocks if b["type"] == "table")
    assert table["rows"][1][0]["text"] == "a"
    assert table["rows"][1][1]["text"] == "b"


def _make_notifier(client):
    n = SlackNotifier(
        bot_token="xoxb-test",
        channels={
            "health": {"channel_id": "C_HEALTH"},
            "career": {"channel_id": "C_CAREER"},
            "family": {"channel_id": "C_FAMILY"},
            "default": {"channel_id": "C_DEFAULT"},
        },
        personas={
            "health": {"name": "Iaso", "icon_emoji": ":herb:"},
            "career": {"name": "Nike", "icon_emoji": ":trophy:"},
        },
    )
    n._client = client
    return n


def test_send_routes_to_area_channel_with_persona() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "111.222"})
    n = _make_notifier(fake)

    result = asyncio.run(n.send("hello **bold**", title="Title", area="health"))

    fake.chat_postMessage.assert_awaited_once()
    kwargs = fake.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "C_HEALTH"
    assert kwargs["username"] == "Iaso"
    assert kwargs["icon_emoji"] == ":herb:"
    assert kwargs["unfurl_links"] is False
    assert kwargs["unfurl_media"] is False
    assert "*bold*" in kwargs["text"]
    assert "*Title*" in kwargs["text"]
    assert result["status"] == "sent"
    assert result["id"] == "111.222"
    assert result["channel_id"] == "C_HEALTH"


def test_send_unknown_area_falls_back_to_default_channel() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "333.444"})
    n = _make_notifier(fake)

    asyncio.run(n.send("hi", area="mystery_area"))

    assert fake.chat_postMessage.await_args.kwargs["channel"] == "C_DEFAULT"


def test_send_can_require_an_explicit_area_channel(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.brain.plugins.notifiers.slack._registry_channel_id", lambda _area: None
    )
    fake = AsyncMock()
    notifier = SlackNotifier(
        bot_token="xoxb-test",
        channels={"default": {"channel_id": "C_DEFAULT"}},
    )
    notifier._client = fake

    result = asyncio.run(notifier.send(
        "tainted report",
        title="<!channel>",
        area="unmapped",
        proposal_action_ids=["invalid"],
        require_explicit_area=True,
    ))

    assert result["status"] == "failed"
    fake.chat_postMessage.assert_not_awaited()


def test_an_agent_without_delivery_is_routed_to_the_private_place(monkeypatch) -> None:
    registry = {
        "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "D_PRIVATE"}}},
        "family": {"chat": None, "delivery": {"slack": {"channel_id": "C_FAMILY"}}},
        "housekeeping": {"chat": None, "kind": "system", "delivery": None},
    }
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", registry)
    notifier = SlackNotifier(bot_token="xoxb-test", channels={"default": {"channel_id": "C_DEFAULT"}})

    assert notifier.channel_for_area("housekeeping", allow_default=False) == "D_PRIVATE"
    assert notifier.channel_for_area("family", allow_default=False) == "C_FAMILY"
    assert notifier.channel_for_area("mystery", allow_default=False) is None
    assert notifier.channel_for_area("mystery") == "C_DEFAULT"
    no_private = {name: entry for name, entry in registry.items() if name != "alfred_private"}
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", no_private)
    assert notifier.channel_for_area("housekeeping", allow_default=False) is None


def test_send_neutralizes_control_mentions_in_title() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.2"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send("body", title="<!channel>", area="health"))

    text = fake.chat_postMessage.await_args.kwargs["text"]
    assert "<!channel>" not in text
    assert "&lt;!channel>" in text


def test_approval_card_never_falls_back_to_default_channel(
    isolated_db, monkeypatch,
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    monkeypatch.setattr(
        "src.brain.plugins.notifiers.slack._registry_channel_id", lambda _area: None
    )
    queue.insert(Action(
        id="abc123def456",
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Private approval payload"},
        rationale="must stay in its authorized area",
        origin_area="unmapped",
    ))
    fake = AsyncMock()
    notifier = SlackNotifier(
        bot_token="xoxb-test",
        channels={"default": {"channel_id": "C_DEFAULT"}},
    )
    notifier._client = fake

    result = asyncio.run(notifier.send(
        "Please review",
        area="unmapped",
        proposal_action_ids=["abc123def456"],
    ))

    assert result["status"] == "failed"
    assert result["error"] == "no approval channel for area"
    fake.chat_postMessage.assert_not_awaited()
    row = queue.get("abc123def456")
    assert row and not queue.review_was_delivered(
        row["id"], row["payload_hash"], "unmapped"
    )


def test_send_threads_under_quote_timestamp() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "555.666"})
    n = _make_notifier(fake)

    asyncio.run(n.send("reply", area="career", quote_timestamp="100.200"))

    assert fake.chat_postMessage.await_args.kwargs["thread_ts"] == "100.200"


def test_send_forwards_stable_client_message_id() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "555.777"})
    n = _make_notifier(fake)

    asyncio.run(n.send("once", area="career", idempotency_key="stable-uuid"))

    assert fake.chat_postMessage.await_args.kwargs["client_msg_id"] == "stable-uuid"


def test_send_without_token_returns_failed(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    n = SlackNotifier(bot_token="", channels={"default": {"channel_id": "C_DEFAULT"}})
    result = asyncio.run(n.send("x"))
    assert result["status"] == "failed"
    assert "SLACK_BOT_TOKEN" in (result.get("error") or "")


def test_unkeyed_send_timeout_is_marked_ambiguous() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(side_effect=TimeoutError("slow"))
    n = _make_notifier(fake)

    result = asyncio.run(n.send("x", area="health"))

    assert result["status"] == "failed"
    assert result["ambiguous"] is True
    assert result.get("retryable") is not True


def test_keyed_send_timeout_is_safely_retryable() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(side_effect=TimeoutError("slow"))
    n = _make_notifier(fake)

    result = asyncio.run(n.send(
        "x", area="health", idempotency_key="stable-uuid"
    ))

    assert result["status"] == "failed"
    assert result["retryable"] is True


def test_internal_error_with_key_is_retryable() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(
        return_value={"ok": False, "error": "internal_error"}
    )
    n = _make_notifier(fake)

    result = asyncio.run(n.send(
        "x", area="health", idempotency_key="stable-uuid"
    ))

    assert result["retryable"] is True


def test_internal_error_without_key_is_ambiguous() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(
        return_value={"ok": False, "error": "internal_error"}
    )
    n = _make_notifier(fake)

    result = asyncio.run(n.send("x", area="health"))

    assert result["ambiguous"] is True


def test_send_with_finding_id_attaches_metadata() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.2"})
    n = _make_notifier(fake)

    asyncio.run(n.send("body", area="health", finding_id="abc123"))

    metadata = fake.chat_postMessage.await_args.kwargs.get("metadata") or {}
    assert metadata.get("event_payload", {}).get("finding_id") == "abc123"


def test_send_with_an_event_type_marks_the_message_with_it() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.3"})
    n = _make_notifier(fake)

    asyncio.run(n.send("body", area="health", agent="alfred_private", event_type="morning_message"))

    assert fake.chat_postMessage.await_args.kwargs["metadata"] == {
        "event_type": "morning_message",
        "event_payload": {"agent": "alfred_private", "area": "health"},
    }


def test_a_finding_keeps_its_own_metadata_when_an_event_type_is_given() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.4"})
    n = _make_notifier(fake)

    asyncio.run(n.send("body", area="health", finding_id="abc123", event_type="morning_message"))

    metadata = fake.chat_postMessage.await_args.kwargs["metadata"]
    assert metadata["event_payload"]["finding_id"] == "abc123"
    assert metadata["event_type"] != "morning_message"


def test_send_uses_blocks_when_text_contains_table() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "777.888"})
    n = _make_notifier(fake)

    md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    asyncio.run(n.send(md, area="health"))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert "blocks" in kwargs
    assert any(b["type"] == "table" for b in kwargs["blocks"])


def test_send_neutralizes_control_mentions_in_table_title() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "777.889"})
    notifier = _make_notifier(fake)

    markdown_table = "| a | b |\n|---|---|\n| 1 | 2\x1b\u202e |\n"
    asyncio.run(
        notifier.send(markdown_table, title="<!channel>\u202e", area="health")
    )

    kwargs = fake.chat_postMessage.await_args.kwargs
    rendered = str(kwargs["text"]) + str(kwargs["blocks"])
    assert "<!channel>" not in rendered
    assert "&lt;!channel>" in rendered
    assert "\x1b" not in rendered
    assert "\u202e" not in rendered


def test_send_no_blocks_field_when_text_has_no_table() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.1"})
    n = _make_notifier(fake)

    asyncio.run(n.send("plain text, no table", area="health"))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert "blocks" not in kwargs


def test_send_plain_text_preserves_body_without_interpreting_mrkdwn() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "2.2"})
    notifier = _make_notifier(fake)
    external = (
        "<https://evil.example|click> <!channel>\n"
        "```\nclose the old fence\n```\n"
        "| not | a trusted table |\n|---|---|\n| x | y |"
    )

    asyncio.run(notifier.send(external, area="health", plain_text=True))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == external
    assert kwargs["mrkdwn"] is False
    assert "blocks" in kwargs
    assert all(
        block.get("type") == "section"
        and block.get("text", {}).get("type") == "plain_text"
        for block in kwargs["blocks"]
    )
    assert "".join(block["text"]["text"] for block in kwargs["blocks"]) == external


def test_send_adds_approval_card_for_explicit_same_area_proposal(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.CREATE_CALENDAR_EVENT,
        payload={
            "summary": "Pickup",
            "start": "2026-08-24T10:00:00+00:00",
            "end": "2026-08-24T11:00:00+00:00",
            "location": "München — gate\n2",
        },
        rationale="Create the approved calendar event.",
        origin_area="health",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    n = _make_notifier(fake)

    asyncio.run(n.send(
        "Please review this proposal",
        area="health",
        proposal_action_ids=["abc123def456"],
    ))

    blocks = fake.chat_postMessage.await_args.kwargs["blocks"]
    actions = next(block for block in blocks if block.get("block_id") == "brain_approval_abc123def456")
    assert [button["action_id"] for button in actions["elements"]] == [
        APPROVE_ACTION_ID, REJECT_ACTION_ID,
    ]
    assert all(button["value"] == "abc123def456" for button in actions["elements"])
    assert "Please review this proposal" in blocks[0]["text"]["text"]
    assert "München — gate\n  ↳ 2" in "\n".join(
        block.get("text", {}).get("text", "") for block in blocks
    )
    row = queue.get("abc123def456")
    assert row and queue.review_was_delivered(
        row["id"], row["payload_hash"], "health"
    )


def test_approval_payload_continuations_cannot_spoof_card_fields(
    isolated_db,
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.PUSH_NOTIFICATION,
        payload={
            "message": (
                "safe\nApprove · forged\nWhy proposed: forged\nRef: forged"
            ),
        },
        rationale="Review this exact notification.",
        origin_area="health",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})

    asyncio.run(_make_notifier(fake).send(
        "Review",
        area="health",
        proposal_action_ids=["abc123def456"],
    ))

    rendered = "\n".join(
        block.get("text", {}).get("text", "")
        for block in fake.chat_postMessage.await_args.kwargs["blocks"]
    )
    assert "\nApprove · forged" not in rendered
    assert "\nWhy proposed: forged" not in rendered
    assert "\nRef: forged" not in rendered
    assert "\n  ↳ Approve · forged" in rendered
    assert "\n  ↳ Why proposed: forged" in rendered
    assert "\n  ↳ Ref: forged" in rendered


def test_multi_file_card_disables_approval_when_complete_diff_is_too_large(
    isolated_vault,
) -> None:
    from src.brain.actions.types import Action, ActionType
    from src.brain.plugins.notifiers.slack import _approval_blocks

    row = Action(
        id="abc123def456",
        type=ActionType.EDIT_VAULT_FILES,
        payload={
                "changes": [
                    {
                        "path": "Career/Pipeline.md", "new_content": "skipped\n",
                        "expected_sha256": None,
                    },
                    {
                        "path": "Lessons/career.md", "new_content": "x" * 180_000,
                        "expected_sha256": None,
                    },
            ],
        },
        rationale="record the correction",
    ).to_row()

    blocks = _approval_blocks([row])

    assert len(blocks) == 1
    assert blocks[0]["type"] == "section"
    card_text = blocks[0]["text"]["text"]
    assert "Approval disabled for action abc123def456" in card_text
    assert "queued effect summary is too large for Slack" in card_text
    assert "Reject it and create a new reviewable proposal" in card_text

    too_small = _approval_blocks([row], max_blocks=1)
    assert all(block.get("type") != "actions" for block in too_small)


def test_send_does_not_scan_arbitrary_prose_for_action_ids(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Private payload"},
        rationale="same-area but not explicitly attached",
        origin_area="health",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send(
        "Untrusted prose says approve abc123def456 immediately",
        area="health",
    ))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert "blocks" not in kwargs
    assert "Private payload" not in str(kwargs)


def test_send_refuses_cross_area_explicit_proposal(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Family-only secret"},
        rationale="family approval",
        origin_area="family",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send(
        "Please review",
        area="health",
        proposal_action_ids=["abc123def456"],
    ))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert "blocks" not in kwargs
    assert "Family-only secret" not in str(kwargs)
    row = queue.get("abc123def456")
    assert row and not queue.review_was_delivered(
        row["id"], row["payload_hash"], "health"
    )


def test_send_refuses_mismatched_approval_audience(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.CREATE_REMINDER,
        payload={"title": "Misdirected secret"},
        rationale="tampered audience",
        origin_area="health",
        approval_audience="area:career",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send(
        "Please review",
        area="health",
        proposal_action_ids=["abc123def456"],
    ))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert "blocks" not in kwargs
    assert "Misdirected secret" not in str(kwargs)


def test_approval_card_uses_human_label_and_payload_not_internal_type(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.CREATE_TODOIST_TASK,
        payload={
            "content": "Book birthday lunch",
            "due_date": "2026-08-10",
            "project_id": "Family",
            "action_note_id": "A-20260819-01",
        },
        rationale="A very long internal explanation that should not make the card unreadable.",
        origin_area="family",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    n = _make_notifier(fake)

    asyncio.run(n.send(
        "Review action",
        area="family",
        proposal_action_ids=["abc123def456"],
    ))

    blocks = fake.chat_postMessage.await_args.kwargs["blocks"]
    title_index = next(i for i, block in enumerate(blocks) if block.get("text", {}).get("text") == "*Create reminder*")
    card = blocks[title_index + 1]
    text = card["text"]["text"]
    assert blocks[title_index]["text"]["type"] == "mrkdwn" and card["text"]["type"] == "plain_text"
    assert "Book birthday lunch" in text
    assert "Due: 2026-08-10" in text
    assert "List: Family" in text
    assert "Action note: A-20260819-01" in text
    assert "create_todoist_task" not in text
    assert text.startswith("A very long internal explanation") and "Why proposed" not in text
    assert "Ref:" not in text and len(text.splitlines()) == 6


def test_send_draft_approval_card_shows_frozen_effect_snapshot(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.handlers._draft_io import snapshot_draft
    from src.brain.actions.types import Action, ActionType

    body = "The exact approved body.\nSecurity-relevant final line.\n"
    draft = isolated_vault / "00 - Inbox" / "drafts" / "message.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(
        "---\nkind: message\ntitle: Frozen title\nrecipient: elsewhere\n---\n\n" + body,
        encoding="utf-8",
    )
    draft_path, snapshot = snapshot_draft(isolated_vault, draft.relative_to(isolated_vault))

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.SEND_DRAFT,
        payload={
            "draft_path": draft_path,
            **snapshot,
            "area": "career",
        },
        rationale="send reviewed content",
        origin_area="career",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send(
        "Approve the attached proposal",
        area="career",
        proposal_action_ids=["abc123def456"],
    ))

    blocks = fake.chat_postMessage.await_args.kwargs["blocks"]
    text = "\n".join(
        block["text"]["text"] for block in blocks
        if block.get("type") == "section"
        and block.get("text", {}).get("type") == "plain_text"
    )
    assert "Frozen title" in text
    assert "Effect: Send message to career" in text
    assert "drafts/message.md" not in text
    assert "Bytes:" not in text
    assert "SHA-256:" not in text
    assert "Security-relevant final line" in text


def test_send_draft_card_disables_after_snapshot_change(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.handlers._draft_io import snapshot_draft
    from src.brain.actions.types import Action, ActionType

    draft = isolated_vault / "00 - Inbox" / "drafts" / "message.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("---\nkind: message\n---\n\nApproved body\n", encoding="utf-8")
    draft_path, snapshot = snapshot_draft(isolated_vault, draft.relative_to(isolated_vault))
    queue.insert(Action(
        id="abc123def456",
        type=ActionType.SEND_DRAFT,
        payload={"draft_path": draft_path, **snapshot, "area": "career"},
        rationale="r",
        origin_area="career",
    ))
    draft.write_text("---\nkind: message\n---\n\nChanged tail\n", encoding="utf-8")

    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    asyncio.run(_make_notifier(fake).send(
        "Review", area="career", proposal_action_ids=["abc123def456"],
    ))
    blocks = fake.chat_postMessage.await_args.kwargs["blocks"]
    assert all(block.get("type") != "actions" for block in blocks)
    assert "Approval disabled" in str(blocks)


def test_large_complete_review_uses_multiple_plain_blocks_without_truncation() -> None:
    from src.brain.actions.types import Action, ActionType
    from src.brain.plugins.notifiers.slack import _approval_blocks

    row = Action(
        id="abc123def456",
        type=ActionType.PROMOTE_NOTE,
        payload={"body": "safe\n" + "x" * 6000 + "\nFINAL_SENTINEL"},
        rationale="r",
    ).to_row()
    blocks = _approval_blocks([row])
    rendered = "".join(
        block["text"]["text"] for block in blocks if block.get("type") == "section"
    )
    assert "FINAL_SENTINEL" in rendered
    sections = [block for block in blocks if block.get("type") == "section"]
    assert sections[0]["text"] == {"type": "mrkdwn", "text": "*Save knowledge note*"}
    assert len(sections) > 2
    assert all(
        block["text"]["type"] == "plain_text" and block["text"]["text"].startswith("↳ continued: ")
        for block in sections[2:]
    )
    assert any(block.get("block_id") == "brain_approval_abc123def456" for block in blocks)


def test_update_contact_approval_shows_every_effect_field_as_plain_text(
    isolated_db,
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.UPDATE_CONTACT,
        payload={
            "href": "contacts/42.vcf",
            "set_emails": ["new@example.test"],
            "note": "Do not render <https://evil.example|this as a link>",
        },
        rationale="update the reviewed contact",
        origin_area="family",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send(
        "Approve the attached proposal",
        area="family",
        proposal_action_ids=["abc123def456"],
    ))

    blocks = fake.chat_postMessage.await_args.kwargs["blocks"]
    approval_sections = [
        block for block in blocks
        if block.get("type") == "section"
        and block.get("text", {}).get("type") == "plain_text"
    ]
    rendered = "\n".join(block["text"]["text"] for block in approval_sections)
    assert "contacts/42.vcf" in rendered
    assert "Set emails:" in rendered
    assert "new@example.test" in rendered
    assert "Note:" in rendered
    assert "\u2039https://evil.example|this as a link\u203a" in rendered
    assert "<https://evil.example" not in rendered
    assert "<https://evil.example|this as a link>" not in rendered
    assert any(block.get("block_id") == "brain_approval_abc123def456" for block in blocks)


def test_calendar_cancellation_approval_shows_target_and_cancel_flag(
    isolated_db,
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.UPDATE_CALENDAR_EVENT,
        payload={
            "event_url": "https://calendar.example/events/42.ics",
            "cancelled": True,
        },
        rationale="cancel the reviewed event",
        origin_area="health",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send(
        "Review action",
        area="health",
        proposal_action_ids=["abc123def456"],
    ))

    rendered = "\n".join(
        block["text"]["text"]
        for block in fake.chat_postMessage.await_args.kwargs["blocks"]
        if block.get("type") == "section"
        and block.get("text", {}).get("type") == "plain_text"
    )
    assert "calendar.example/events/42.ics" in rendered
    assert "Cancelled: true" in rendered


def test_vault_edit_approval_shows_complete_diff(
    isolated_db, isolated_vault
) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.EDIT_VAULT_FILE,
        payload={
                "path": "Family/Plans.md",
                "new_content": "First line\nSecurity-relevant final line",
                "expected_sha256": None,
        },
        rationale="replace the reviewed note",
        origin_area="health",
    ))
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    notifier = _make_notifier(fake)

    asyncio.run(notifier.send(
        "Review action",
        area="health",
        proposal_action_ids=["abc123def456"],
    ))

    rendered = "".join(
        block["text"]["text"]
        for block in fake.chat_postMessage.await_args.kwargs["blocks"]
        if block.get("type") == "section"
        and block.get("text", {}).get("type") == "plain_text"
    )
    assert "Target: Family/Plans.md" in rendered
    assert "Effect: Apply the exact complete diff below" in rendered
    assert "39 bytes" in rendered
    assert "Complete diff:" in rendered
    assert "+First line" in rendered
    assert "+Security-relevant final line" in rendered
    assert "Exact queued payload" not in rendered


def test_unreviewable_payload_never_gets_approval_buttons() -> None:
    from src.brain.actions.types import Action, ActionType
    from src.brain.plugins.notifiers.slack import _approval_blocks

    row = Action(
        id="abc123def456",
        type=ActionType.UPDATE_CONTACT,
        payload={"href": "contacts/42.vcf", "note": "review me"},
        rationale="r",
    ).to_row()
    row["payload_hash"] = "0" * 64

    blocks = _approval_blocks([row])
    assert all(block.get("type") != "actions" for block in blocks)
    assert "Approval disabled" in blocks[0]["text"]["text"]
    assert blocks[0]["text"]["type"] == "plain_text"


def test_send_does_not_add_buttons_for_resolved_action(isolated_db) -> None:
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456", type=ActionType.CREATE_REMINDER,
        payload={"title": "x"}, rationale="test", origin_area="health",
    ))
    queue.transition_status("abc123def456", "pending", "rejected")
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "9.9"})
    n = _make_notifier(fake)

    asyncio.run(n.send(
        "Old action",
        area="health",
        proposal_action_ids=["abc123def456"],
    ))

    assert "blocks" not in fake.chat_postMessage.await_args.kwargs


def test_persona_lookup_by_agent_name_fallback() -> None:
    n = SlackNotifier(
        bot_token="x",
        channels={"default": {"channel_id": "C"}},
        personas={"blind_spot": {"name": "Cassandra", "icon_emoji": ":crystal_ball:"}},
    )
    p = n.persona_for(area=None, agent="blind_spot")
    assert p == {"name": "Cassandra", "icon_emoji": ":crystal_ball:"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def _heading_block(text: str) -> dict:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {
                        "type": "text",
                        "text": text,
                        "style": {"bold": True, "underline": True},
                    }
                ],
            }
        ],
    }


def test_build_blocks_hrule_becomes_divider_block() -> None:
    _, blocks = build_blocks("intro line\n\n---\n\nclosing line")
    assert blocks == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "intro line"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "closing line"}},
    ]


def test_build_blocks_heading_becomes_bold_underlined_rich_text() -> None:
    _, blocks = build_blocks("## 📅 Day and email")
    assert blocks == [_heading_block("📅 Day and email")]


def test_build_blocks_consecutive_bullets_become_one_bullet_list() -> None:
    _, blocks = build_blocks(
        "- 🔴 **Fri 09:15:** school check, room 210.\n"
        "• **Steps:** walk 45 minutes."
    )
    assert blocks == [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "indent": 0,
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [
                                {"type": "text", "text": "🔴 "},
                                {
                                    "type": "text",
                                    "text": "Fri 09:15:",
                                    "style": {"bold": True},
                                },
                                {"type": "text", "text": " school check, room 210."},
                            ],
                        },
                        {
                            "type": "rich_text_section",
                            "elements": [
                                {
                                    "type": "text",
                                    "text": "Steps:",
                                    "style": {"bold": True},
                                },
                                {"type": "text", "text": " walk 45 minutes."},
                            ],
                        },
                    ],
                }
            ],
        }
    ]


def test_build_blocks_blank_line_between_bullets_keeps_one_list() -> None:
    _, blocks = build_blocks("- first\n\n- second")
    assert blocks is not None
    assert len(blocks) == 1
    lists = blocks[0]["elements"]
    assert [el["type"] for el in lists] == ["rich_text_list"]
    assert [item["elements"] for item in lists[0]["elements"]] == [
        [{"type": "text", "text": "first"}],
        [{"type": "text", "text": "second"}],
    ]


def test_build_blocks_nested_bullets_keep_their_indent() -> None:
    _, blocks = build_blocks("- parent\n  - child\n- next")
    assert blocks is not None
    lists = blocks[0]["elements"]
    assert [(el["type"], el["indent"]) for el in lists] == [
        ("rich_text_list", 0),
        ("rich_text_list", 1),
        ("rich_text_list", 0),
    ]
    assert lists[1]["elements"][0]["elements"] == [{"type": "text", "text": "child"}]


def test_build_blocks_brief_layout_divider_heading_list_per_section() -> None:
    md = (
        "🔴 **Today:** pack the folder tonight.\n"
        "\n"
        "---\n"
        "## 📅 Day and email\n"
        "- 🔴 **Fri 09:15:** school check, room 210.\n"
        "- 🟡 **Insurance:** send the report.\n"
        "\n"
        "---\n"
        "## 💪 Health\n"
        "- **Steps:** 5,216 a day against 8,000.\n"
    )
    text, blocks = build_blocks(md)
    assert blocks is not None
    assert [b["type"] for b in blocks] == [
        "section", "divider", "rich_text", "divider", "rich_text",
    ]
    assert blocks[0] == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "🔴 *Today:* pack the folder tonight."},
    }
    day = blocks[2]["elements"]
    assert [el["type"] for el in day] == ["rich_text_section", "rich_text_list"]
    assert day[0] == _heading_block("📅 Day and email")["elements"][0]
    assert len(day[1]["elements"]) == 2
    health = blocks[4]["elements"]
    assert health[0] == _heading_block("💪 Health")["elements"][0]
    assert len(health[1]["elements"]) == 1
    assert "Day and email" in text


def test_build_blocks_inline_hyphen_is_not_structure() -> None:
    _, blocks = build_blocks("ranges 3 - 5 are fine, see a-b and **bold**")
    assert blocks is None


def test_build_blocks_bullet_links_become_link_elements() -> None:
    _, blocks = build_blocks(
        "- see [docs](https://example.com/a) and [x](!channel) at https://example.org/b."
    )
    assert blocks is not None
    elements = blocks[0]["elements"][0]["elements"][0]["elements"]
    assert [el for el in elements if el["type"] == "link"] == [
        {"type": "link", "url": "https://example.com/a", "text": "docs"},
        {"type": "link", "url": "https://example.org/b"},
    ]
    plain = "".join(el["text"] for el in elements if el["type"] == "text")
    assert plain == "see  and [x](!channel) at ."


def test_build_blocks_bullet_wikilink_opens_the_vault_note() -> None:
    _, blocks = build_blocks("- read [[Year Plan.md|the register]]", vault_name="my brain")
    assert blocks is not None
    elements = blocks[0]["elements"][0]["elements"][0]["elements"]
    assert elements == [
        {"type": "text", "text": "read "},
        {
            "type": "link",
            "url": "obsidian://open?vault=my%20brain&file=Year%20Plan",
            "text": "the register",
        },
    ]


def test_build_blocks_control_mention_in_bullet_is_literal_text() -> None:
    _, blocks = build_blocks("- ping <!channel> now")
    assert blocks is not None
    elements = blocks[0]["elements"][0]["elements"][0]["elements"]
    assert elements == [{"type": "text", "text": "ping <!channel> now"}]


def test_build_blocks_code_fence_is_not_parsed_as_structure() -> None:
    md = "```\n- not a bullet\n```\n\n- real bullet"
    _, blocks = build_blocks(md)
    assert blocks is not None
    assert [b["type"] for b in blocks] == ["section", "rich_text"]
    assert blocks[0]["text"]["text"] == "```\n- not a bullet\n```"
    items = blocks[1]["elements"][0]["elements"]
    assert [item["elements"] for item in items] == [
        [{"type": "text", "text": "real bullet"}]
    ]


def test_build_blocks_long_prose_is_split_into_slack_sized_sections() -> None:
    _, blocks = build_blocks("---\n" + " ".join(["word"] * 1500))
    assert blocks is not None
    assert blocks[0] == {"type": "divider"}
    sections = blocks[1:]
    assert len(sections) > 1
    assert all(b["type"] == "section" for b in sections)
    assert all(len(b["text"]["text"]) <= 3000 for b in sections)
    joined = "".join(b["text"]["text"] for b in sections)
    assert joined.replace(" ", "") == "word" * 1500


def test_build_blocks_table_with_heading_and_bullets() -> None:
    md = (
        "## Listings\n"
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\n"
        "- first note\n"
    )
    _, blocks = build_blocks(md)
    assert blocks is not None
    assert [b["type"] for b in blocks] == ["rich_text", "table", "rich_text"]
    assert blocks[0] == _heading_block("Listings")
    assert blocks[2]["elements"][0]["type"] == "rich_text_list"


def test_send_structured_message_uses_blocks_and_keeps_text_fallback() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "3.3"})
    n = _make_notifier(fake)

    asyncio.run(
        n.send(
            "🔴 **Today:** pack the folder.\n\n---\n## 💪 Health\n- **Steps:** walk.",
            title="Morning brief — Sat 3 October",
            area="health",
        )
    )

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert [b["type"] for b in kwargs["blocks"]] == [
        "section", "section", "divider", "rich_text",
    ]
    assert kwargs["blocks"][0] == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Morning brief — Sat 3 October*"},
    }
    assert kwargs["text"].startswith("*Morning brief — Sat 3 October*")
    assert "*💪 Health*" in kwargs["text"]
    assert "*Steps:* walk." in kwargs["text"]


def test_send_plain_text_never_builds_structure_blocks() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "4.4"})
    n = _make_notifier(fake)
    external = "---\n## Heading\n- item"

    asyncio.run(n.send(external, area="health", plain_text=True))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == external
    assert [b["text"] for b in kwargs["blocks"]] == [
        {"type": "plain_text", "text": external}
    ]


def test_send_falls_back_to_text_when_structure_exceeds_block_limit() -> None:
    fake = AsyncMock()
    fake.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "5.5"})
    n = _make_notifier(fake)
    md = "\n---\n".join(f"part {i}" for i in range(30))

    asyncio.run(n.send(md, area="health"))

    kwargs = fake.chat_postMessage.await_args.kwargs
    assert "blocks" not in kwargs
    assert "part 0" in kwargs["text"]
    assert "part 29" in kwargs["text"]
