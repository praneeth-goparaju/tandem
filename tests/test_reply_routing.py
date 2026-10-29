from __future__ import annotations

import asyncio
import importlib
import logging
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.brain.media.voice import ClipOverLimit
from src.brain.runtime.loader import LoadedConfig
from src.orchestrator import reply_routing


def test_sender_identity_owner_via_env(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {})
    assert reply_routing.sender_identity("UOWNER") == {
        "name": "owner", "areas": "*", "is_owner": True,
    }


def test_sender_identity_unauthorized_returns_none(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {})
    assert reply_routing.sender_identity("USOMEONE") is None


def test_sender_identity_authorized_dict_entry(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {
        "authorized_senders": {
            "UCOOWNER": {"name": "Co-owner", "areas": ["family", "health"]},
        }
    })
    assert reply_routing.sender_identity("UCOOWNER") == {
        "name": "Co-owner",
        "areas": ["family", "health"],
        "is_owner": False,
    }


def test_sender_identity_explicit_empty_areas_grants_nothing(monkeypatch):
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {
        "authorized_senders": {"UNONE": {"name": "Observer", "areas": []}}
    })
    identity = reply_routing.sender_identity("UNONE")
    assert identity == {"name": "Observer", "areas": [], "is_owner": False}
    assert not reply_routing.sender_allowed_for_area(identity, "family")
    assert not reply_routing.sender_allowed_for_area(identity, None)


@pytest.mark.parametrize("areas", [None, "family", ["family", 7]])
def test_sender_identity_malformed_areas_never_becomes_wildcard(monkeypatch, areas):
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {
        "authorized_senders": {"UBAD": {"name": "Broken", "areas": areas}}
    })

    identity = reply_routing.sender_identity("UBAD")

    assert identity is not None
    assert not reply_routing.sender_allowed_for_area(identity, "family")
    assert not reply_routing.sender_allowed_for_area(identity, None)


def test_sender_identity_empty_string():
    assert reply_routing.sender_identity("") is None


def test_sender_identity_rejects_implicit_scope_and_owner_display_name(monkeypatch):
    monkeypatch.delenv("OWNER_SLACK_USER_ID", raising=False)
    for entry in ("Owner", {"name": "owner"}):
        monkeypatch.setattr(reply_routing, "_notifier_options", lambda entry=entry: {
            "authorized_senders": {"UIMPOSTOR": entry},
        })
        assert reply_routing.sender_identity("UIMPOSTOR") is None


def test_sender_allowed_wildcard():
    identity = {"name": "co-owner", "areas": "*", "is_owner": False}
    assert reply_routing.sender_allowed_for_area(identity, "anything")
    assert not reply_routing.sender_allowed_for_area(identity, None)


def test_sender_allowed_scoped_by_list():
    identity = {"name": "Co-owner", "areas": ["family", "health"]}
    assert reply_routing.sender_allowed_for_area(identity, "family")
    assert not reply_routing.sender_allowed_for_area(identity, "career")


def test_sender_allowed_no_area_only_owner_floats():
    owner = {"name": "owner", "areas": ["health"], "is_owner": True}
    other = {"name": "owner", "areas": ["health"], "is_owner": False}
    assert reply_routing.sender_allowed_for_area(owner, None)
    assert not reply_routing.sender_allowed_for_area(other, None)


SHARED_MEMBERS = ["alfred", "family", "housing"]


def test_owner_may_use_any_place_whatever_their_areas_say():
    owner = {"name": "owner", "areas": [], "is_owner": True}
    assert reply_routing.sender_allowed_for_place(owner, SHARED_MEMBERS, whole_vault=False)
    assert reply_routing.sender_allowed_for_place(owner, ["alfred_private"], whole_vault=True)


@pytest.mark.parametrize("areas", ["*", ["alfred_private", "health"]])
def test_nobody_but_the_owner_may_use_a_run_that_reads_the_whole_vault(areas):
    sender = {"name": "co-owner", "areas": areas, "is_owner": False}
    assert not reply_routing.sender_allowed_for_place(
        sender, ["alfred_private", "health"], whole_vault=True
    )


@pytest.mark.parametrize("areas", [
    "*",
    ["alfred", "family", "housing"],
    ("housing", "family", "alfred"),
    {"alfred", "family", "housing", "health"},
])
def test_shared_place_needs_every_area_of_it_or_a_wildcard(areas):
    sender = {"name": "co-owner", "areas": areas, "is_owner": False}
    assert reply_routing.sender_allowed_for_place(sender, SHARED_MEMBERS, whole_vault=False)


@pytest.mark.parametrize("areas", [
    ["alfred", "family"],
    ["family", "housing"],
    [],
    "family",
    None,
    {"alfred": "family"},
    [["alfred", "family", "housing"]],
    ["alfred", None, "family", "housing"],
])
def test_shared_place_refuses_a_sender_who_lacks_an_area_or_is_malformed(areas):
    sender = {"name": "co-owner", "areas": areas, "is_owner": False}
    assert not reply_routing.sender_allowed_for_place(sender, SHARED_MEMBERS, whole_vault=False)


def test_infer_area_from_group_match(monkeypatch):
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", {
        "health": {"delivery": {"slack": {"channel_id": "C111"}}},
        "career": {"delivery": {"slack": {"channel_id": "C222"}}},
    })
    assert reply_routing._infer_area_from_group("C222") == "career"


def test_infer_area_from_group_no_match(monkeypatch):
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", {
        "health": {"delivery": {"slack": {"channel_id": "C111"}}},
    })
    assert reply_routing._infer_area_from_group("C999") is None


def test_infer_area_from_group_none():
    assert reply_routing._infer_area_from_group(None) is None


def _chat_registry(monkeypatch):
    registry = {
        "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
        "family": {"chat": None, "delivery": {"slack": {"channel_id": "CSHARED"}}},
    }
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", registry)
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", registry)
    return registry


def test_shared_channel_keeps_all_areas_and_routes_top_level_to_the_chat_agent(
    monkeypatch,
):
    _chat_registry(monkeypatch)

    assert reply_routing._areas_from_group("CSHARED") == {"alfred", "family"}
    assert reply_routing._infer_area_from_group("CSHARED") == "alfred"


PLACES_REGISTRY = {
    "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "family": {"chat": None, "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "DPRIV"}}},
    "housekeeping": {"chat": None, "kind": "system", "delivery": None},
}


def _places_registry(monkeypatch):
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", PLACES_REGISTRY)
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", PLACES_REGISTRY)


def test_an_agent_without_delivery_is_an_area_of_the_private_channel_only(monkeypatch):
    _places_registry(monkeypatch)

    assert reply_routing._areas_from_group("DPRIV") == {"alfred_private", "housekeeping"}
    assert reply_routing._areas_from_group("CSHARED") == {"alfred", "family"}
    assert reply_routing._infer_area_from_group("DPRIV") == "alfred_private"


def test_shared_channel_without_a_chat_agent_has_no_top_level_route(monkeypatch):
    registry = {
        "assistant": {"delivery": {"slack": {"channel_id": "CASK"}}},
        "blind_spot": {"delivery": {"slack": {"channel_id": "CASK"}}},
    }
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", registry)
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", registry)
    assert reply_routing._areas_from_group("CASK") == {"assistant", "blind_spot"}
    assert reply_routing._infer_area_from_group("CASK") is None


@pytest.fixture
def routing_with_db(isolated_db, monkeypatch):
    importlib.reload(reply_routing)
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {})
    yield isolated_db


def _seed_notification(conn, *, agent, area, finding_id, ts):
    conn.execute(
        "INSERT INTO notifications "
        "(id, agent, title, message, status, finding_id, signal_timestamp, area, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"n_{ts}", agent, "t", "m", "sent", finding_id, ts, area, "2026-04-28T00:00:00"),
    )
    conn.commit()


def _seed_finding(conn, *, finding_id, title, detail, agent="discovery"):
    conn.execute(
        "INSERT INTO findings (id, agent, category, title, detail, created_at) "
        "VALUES (?, ?, 'IMPORTANT', ?, ?, ?)",
        (finding_id, agent, title, detail, "2026-04-28T00:00:00"),
    )
    conn.commit()


def _propose_for_area(area: str) -> str:
    from src.brain.actions import queue
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.actions import propose_action

    with caller_scope(ToolCaller(
        agent=area,
        area=area,
        allowed_action_types=frozenset({"push_notification"}),
    )):
        asyncio.run(propose_action({
            "type": "push_notification",
            "payload": {"title": "t", "message": "m"},
            "rationale": "needs a tap",
            "risk": "confirm",
        }))
    row = queue.list_by_status("pending", origin_area=area)[0]
    queue.record_review_delivery(
        row["id"], row["payload_hash"], area, f"C{area.upper()}", "1.1"
    )
    return row["id"]


def test_resolve_by_finding_id_directly(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="health", area="health", finding_id="F1", ts="100.1")
    _seed_finding(conn, finding_id="F1", title="Pollen high", detail="Take meds")

    ctx = reply_routing.resolve_reply_context({"finding_id": "F1"})
    assert ctx["agent"] == "health"
    assert ctx["area"] == "health"
    assert ctx["finding_id"] == "F1"
    assert ctx["finding_title"] == "Pollen high"
    assert ctx["finding_detail"] == "Take meds"
    assert ctx["finding_agent"] == "discovery"


def test_resolve_by_quoted_timestamp(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="career", area="career", finding_id="F2", ts="200.2")
    _seed_finding(conn, finding_id="F2", title="Job posting", detail="Apply")

    ctx = reply_routing.resolve_reply_context({"quoted_timestamp": "200.2"})
    assert ctx["agent"] == "career"
    assert ctx["area"] == "career"
    assert ctx["finding_id"] == "F2"


def test_resolve_falls_through_to_channel_area(routing_with_db, monkeypatch):
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", {
        "housing": {"delivery": {"slack": {"channel_id": "CHOUSING"}}},
    })
    ctx = reply_routing.resolve_reply_context({"group_id": "CHOUSING"})
    assert ctx["area"] == "housing"
    assert ctx["finding_id"] is None


def test_resolve_dm_without_configured_agent_has_no_agent(routing_with_db):
    ctx = reply_routing.resolve_reply_context({"channel_type": "im"})
    assert ctx["area"] is None
    assert ctx["agent"] is None


def test_resolve_returns_empty_context_on_no_match(routing_with_db):
    ctx = reply_routing.resolve_reply_context({})
    assert ctx["agent"] is None
    assert ctx["area"] is None
    assert ctx["finding_id"] is None
    assert ctx["finding_title"] == ""


def test_resolve_finding_id_with_no_notifications_row(routing_with_db):
    ctx = reply_routing.resolve_reply_context({"finding_id": "F_GONE"})
    assert ctx["finding_id"] == "F_GONE"
    assert ctx["agent"] is None
    assert ctx["area"] is None
    assert ctx["finding_title"] == ""
    assert ctx["finding_detail"] == ""


def test_resolve_finding_id_present_but_findings_row_missing(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="career", area="career", finding_id="F_EXPIRED", ts="500.5")

    ctx = reply_routing.resolve_reply_context({"finding_id": "F_EXPIRED"})
    assert ctx["agent"] == "career"
    assert ctx["area"] == "career"
    assert ctx["finding_id"] == "F_EXPIRED"
    assert ctx["finding_title"] == ""
    assert ctx["finding_detail"] == ""


def test_resolve_quoted_ts_recovers_finding_id_when_caller_omits_it(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="health", area="health", finding_id="F_HYD", ts="600.6")
    _seed_finding(conn, finding_id="F_HYD", title="Pollen", detail="meds")

    ctx = reply_routing.resolve_reply_context({"quoted_timestamp": "600.6"})
    assert ctx["finding_id"] == "F_HYD"
    assert ctx["agent"] == "health"


def test_resolve_quoted_ts_with_null_finding_id_in_notifications(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="assistant", area="assistant", finding_id=None, ts="700.7")

    ctx = reply_routing.resolve_reply_context({
        "finding_id": "F_KEEP",
        "quoted_timestamp": "700.7",
    })
    assert ctx["finding_id"] == "F_KEEP"
    assert ctx["agent"] == "assistant"
    assert ctx["area"] == "assistant"


def test_resolve_finding_id_takes_precedence_over_quoted_ts(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="health", area="health", finding_id="F_DIRECT", ts="800.8")
    _seed_notification(conn, agent="career", area="career", finding_id="F_THREAD", ts="800.9")
    _seed_finding(conn, finding_id="F_DIRECT", title="direct", detail="d")

    ctx = reply_routing.resolve_reply_context({
        "finding_id": "F_DIRECT",
        "quoted_timestamp": "800.9",
    })
    assert ctx["finding_id"] == "F_DIRECT"
    assert ctx["agent"] == "health"
    assert ctx["area"] == "health"


def test_resolve_area_from_channel_promotes_to_agent_when_in_registry(routing_with_db):
    from src.orchestrator.registry import AGENT_REGISTRY
    pick = next(iter(AGENT_REGISTRY))
    registry = {pick: {"delivery": {"slack": {"channel_id": "CPICK"}}}}
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as m:
        m.setattr(reply_routing, "AGENT_REGISTRY", registry)
        ctx = reply_routing.resolve_reply_context({"group_id": "CPICK"})

    assert ctx["area"] == pick
    assert ctx["agent"] == pick


def test_integrations_channels_are_not_an_authorization_fallback(
    routing_with_db, monkeypatch
):
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", {})
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {
        "channels": {"unknown_area": {"channel_id": "CUNK"}}
    })
    ctx = reply_routing.resolve_reply_context({"group_id": "CUNK"})
    assert ctx["area"] is None
    assert ctx["agent"] is None


def test_resolve_finding_detail_truncated_to_400(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="health", area="health", finding_id="F_LONG", ts="900.9")
    _seed_finding(conn, finding_id="F_LONG", title="t", detail="x" * 1000)

    ctx = reply_routing.resolve_reply_context({"finding_id": "F_LONG"})
    assert len(ctx["finding_detail"]) == 400


def test_resolve_dm_with_explicit_finding_id_keeps_finding(routing_with_db):
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="career", area="career", finding_id="F_DM", ts="1.1")

    ctx = reply_routing.resolve_reply_context({
        "finding_id": "F_DM",
        "channel_type": "im",
    })
    assert ctx["area"] == "career"
    assert ctx["agent"] == "career"


def test_thread_with_retired_area_falls_back_to_chat_agent(routing_with_db, monkeypatch):
    _chat_registry(monkeypatch)
    conn = routing_with_db.get_db()
    _seed_notification(conn, agent="blind_spot", area="blind_spot", finding_id=None, ts="1.1")
    _seed_notification(conn, agent="family", area="family", finding_id=None, ts="2.2")

    retired = reply_routing.resolve_reply_context(
        {"quoted_timestamp": "1.1", "group_id": "CSHARED"}
    )
    live = reply_routing.resolve_reply_context(
        {"quoted_timestamp": "2.2", "group_id": "CSHARED"}
    )
    elsewhere = reply_routing.resolve_reply_context(
        {"quoted_timestamp": "1.1", "group_id": "CELSEWHERE"}
    )

    assert (retired["area"], retired["agent"]) == ("alfred", "alfred")
    assert (live["area"], live["agent"]) == ("family", "family")
    assert (elsewhere["area"], elsewhere["agent"]) == ("blind_spot", "blind_spot")


def test_reply_under_a_retired_agent_is_dispatched_to_the_chat_agent(
    routing_with_db, monkeypatch
):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    _chat_registry(monkeypatch)
    monkeypatch.setattr(reply_routing, "route_reply_to_area_agent", AsyncMock())
    _seed_notification(
        routing_with_db.get_db(), agent="assistant", area="assistant",
        finding_id=None, ts="3.3",
    )

    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": "and the dentist?",
        "timestamp": "retired-reply", "quoted_timestamp": "3.3", "group_id": "CSHARED",
    }))

    reply_routing.route_reply_to_area_agent.assert_called_once()
    assert reply_routing.route_reply_to_area_agent.call_args.args[2]["area"] == "alfred"


def test_emoji_actions_cover_both_slack_and_unicode():
    actions = reply_routing.EMOJI_ACTIONS
    assert actions["thumbsup"] == "done"
    assert actions["thumbsdown"] == "skip"
    assert actions["👍"] == "done"
    assert actions["✅"] == "done"
    assert actions["👎"] == "skip"


def _insert_reply_row(conn, *, agent, group_id, created_at):
    conn.execute(
        "INSERT INTO notifications "
        "(id, agent, title, message, status, finding_id, signal_timestamp, area, group_id, created_at) "
        "VALUES (?, ?, '(reply)', 'm', 'sent', NULL, 'tsX', NULL, ?, ?)",
        (f"r_{created_at}_{agent or 'none'}", agent, group_id, created_at),
    )
    conn.commit()


@pytest.fixture
def _owner_env(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    mf = MagicMock()
    monkeypatch.setattr("src.db.mark_finding", mf)
    channel_areas = {
        "CCAREER": "career",
        "CFAMILY": "family",
        "CTEST": "test",
    }
    monkeypatch.setattr(
        reply_routing,
        "_infer_area_from_group",
        lambda group_id: channel_areas.get(group_id),
    )
    monkeypatch.setattr(
        reply_routing,
        "_areas_from_group",
        lambda group_id: frozenset(
            {channel_areas[group_id]} if group_id in channel_areas else set()
        ),
    )
    monkeypatch.setattr(
        reply_routing,
        "AGENT_REGISTRY",
        {
            **reply_routing.AGENT_REGISTRY,
            "career": {},
            "family": {},
            "test": {},
        },
    )
    monkeypatch.setattr(reply_routing, "route_reply_to_area_agent", AsyncMock())
    return mf


def test_on_reply_unauthorized_sender_is_ignored(routing_with_db, _owner_env):
    loader = AsyncMock()
    asyncio.run(reply_routing.on_reply(
        {
            "type": "reply", "sender": "UNOBODY", "text": "done",
            "timestamp": "u1", "image_count": 1, "_load_images": loader,
        }
    ))
    loader.assert_not_awaited()
    _owner_env.assert_not_called()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_on_reply_unauthorized_approval_returns_visible_failure(
    routing_with_db, _owner_env
):
    result = asyncio.run(reply_routing.on_reply({
        "type": "approval",
        "sender": "UNOBODY",
        "decision": "approved",
        "action_id": "abc123def456",
        "timestamp": "unauthorized-button",
    }))

    assert result == {"outcome": "unauthorized", "action_id": "abc123def456"}


def test_owner_reply_on_unmapped_channel_is_rejected(routing_with_db, _owner_env):
    asyncio.run(reply_routing.on_reply({
        "type": "reply",
        "sender": "UOWNER",
        "text": "please handle this",
        "timestamp": "unmapped-reply",
        "group_id": "CUNKNOWN",
    }))

    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_owner_dm_without_configured_fallback_is_rejected(
    routing_with_db, _owner_env, monkeypatch
):
    monkeypatch.setattr(reply_routing, "_notifier_options", lambda: {})

    asyncio.run(reply_routing.on_reply({
        "type": "reply",
        "sender": "UOWNER",
        "text": "please handle this",
        "timestamp": "unmapped-dm",
        "group_id": "DUNKNOWN",
        "channel_type": "im",
    }))

    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_non_owner_direct_message_is_refused_with_pointer(
    routing_with_db, _owner_env, monkeypatch
):
    monkeypatch.setattr(
        reply_routing,
        "sender_identity",
        lambda _sender: {"name": "co-owner", "areas": "*", "is_owner": False},
    )
    send_direct = AsyncMock()
    monkeypatch.setattr(reply_routing, "send_direct", send_direct)
    resolve = MagicMock()
    monkeypatch.setattr(reply_routing, "resolve_reply_context", resolve)
    loader = AsyncMock()

    asyncio.run(reply_routing.on_reply({
        "type": "reply",
        "sender": "UCOOWNER",
        "text": "what is on my list?",
        "timestamp": "coowner-dm",
        "group_id": "DCOOWNER",
        "channel_type": "im",
        "image_count": 1,
        "_load_images": loader,
    }))

    send_direct.assert_awaited_once_with(
        "DCOOWNER", "coowner-dm", "Please write in the shared channel."
    )
    resolve.assert_not_called()
    loader.assert_not_awaited()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def _notifier_with_client(monkeypatch, client):
    monkeypatch.setattr(
        "src.brain.mcp._common.notifier", lambda: SimpleNamespace(_get_client=lambda: client)
    )


def test_send_direct_posts_in_the_channel_with_the_notifier_client(monkeypatch):
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    _notifier_with_client(monkeypatch, client)

    asyncio.run(reply_routing.send_direct("D1", "5.5", "Please write in the shared channel."))
    asyncio.run(reply_routing.send_direct("D1", None, "No thread."))

    assert client.chat_postMessage.await_args_list[0].kwargs == {
        "channel": "D1", "text": "Please write in the shared channel.", "thread_ts": "5.5",
    }
    assert client.chat_postMessage.await_args_list[1].kwargs == {
        "channel": "D1", "text": "No thread.",
    }


def test_send_direct_without_a_slack_client_only_logs(monkeypatch, caplog):
    monkeypatch.setattr("src.brain.mcp._common.notifier", lambda: object())
    caplog.set_level("INFO", logger="brain-scheduler")

    asyncio.run(reply_routing.send_direct("D1", "5.5", "Please write in the shared channel."))

    assert "no Slack client" in caplog.text


def test_send_direct_failure_is_logged_not_raised(monkeypatch, caplog):
    client = MagicMock()
    client.chat_postMessage = AsyncMock(side_effect=RuntimeError("slack is down"))
    _notifier_with_client(monkeypatch, client)

    asyncio.run(reply_routing.send_direct("D1", "5.5", "Please write in the shared channel."))

    assert "slack is down" in caplog.text


def test_reply_context_must_match_physical_channel(
    routing_with_db, _owner_env
):
    _seed_notification(
        routing_with_db.get_db(),
        agent="career",
        area="career",
        finding_id="cross-area-finding",
        ts="cross-area-root",
    )

    asyncio.run(reply_routing.on_reply({
        "type": "reply",
        "sender": "UOWNER",
        "text": "please handle this",
        "timestamp": "cross-area-reply",
        "quoted_timestamp": "cross-area-root",
        "group_id": "CFAMILY",
    }))

    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_on_reply_reaction_marks_finding(routing_with_db, _owner_env):
    _seed_notification(routing_with_db.get_db(), agent="discovery", area="career", finding_id="f99", ts="r-ts-1")
    asyncio.run(reply_routing.on_reply({
        "type": "reaction", "sender": "UOWNER",
        "emoji": "white_check_mark", "target_timestamp": "r-ts-1",
        "group_id": "CCAREER",
    }))
    _owner_env.assert_called_once()
    assert _owner_env.call_args.args[1] == "f99"
    assert _owner_env.call_args.args[2] == "done"


def test_on_reply_unknown_reaction_is_ignored(routing_with_db, _owner_env):
    _seed_notification(routing_with_db.get_db(), agent="discovery", area="career", finding_id="f99", ts="r-ts-2")
    asyncio.run(reply_routing.on_reply({
        "type": "reaction", "sender": "UOWNER",
        "emoji": "tada", "target_timestamp": "r-ts-2",
    }))
    _owner_env.assert_not_called()


def test_on_reply_reaction_with_no_mapped_finding_is_ignored(routing_with_db, _owner_env):
    asyncio.run(reply_routing.on_reply({
        "type": "reaction", "sender": "UOWNER",
        "emoji": "white_check_mark", "target_timestamp": "no-such-ts",
    }))
    _owner_env.assert_not_called()


def test_on_reply_reply_fast_path_marks_done(routing_with_db, _owner_env):
    _seed_notification(routing_with_db.get_db(), agent="discovery", area="career", finding_id="f42", ts="q-ts-1")
    loader = AsyncMock()
    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": "done",
        "timestamp": "reply-1", "quoted_timestamp": "q-ts-1",
        "group_id": "CCAREER", "image_count": 1, "_load_images": loader,
    }))
    loader.assert_not_awaited()
    _owner_env.assert_called_once()
    assert _owner_env.call_args.args[1] == "f42"
    assert _owner_env.call_args.args[2] == "done"
    reply_routing.route_reply_to_area_agent.assert_not_called()


PRIVATE_LINE ="F-000001 Did the Garmin sync get fixed? (asked 14 Oct in Claude Code, brain)"
SHARED_LINE = "F-000001 Who books the dentist? (asked 15 Oct in the shared channel)"
SUMMARY = "Good morning. Two follow-ups need you today."

FOLLOW_UP_PLACES = {
    "alfred": {"chat": "shared", "delivery": {"slack": {"channel_id": "CSHARED"}}},
    "family": {"delivery": {"slack": {"channel_id": "CSHARED"}}},
    "alfred_private": {"chat": "private", "delivery": {"slack": {"channel_id": "DOWNER"}}},
    "health": {"delivery": {"slack": {"channel_id": "DOWNER"}}},
    "follow_up": {"kind": "system", "delivery": None},
}
CO_OWNER = {"UCOOWNER": {"name": "Sam", "areas": ["alfred", "family"]}}
SHARED_CHANNEL, DIRECT_MESSAGES = "CSHARED", "DOWNER"


@pytest.fixture
def places(routing_with_db, monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", FOLLOW_UP_PLACES)
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", FOLLOW_UP_PLACES)
    monkeypatch.setattr(
        reply_routing, "_notifier_options", lambda: {"authorized_senders": CO_OWNER}
    )
    monkeypatch.setattr(reply_routing, "route_reply_to_area_agent", AsyncMock())
    monkeypatch.setattr(reply_routing, "send_fallback", AsyncMock())
    for channel, area in ((SHARED_CHANNEL, "alfred"), (DIRECT_MESSAGES, "alfred_private")):
        _seed_notification(
            routing_with_db.get_db(), agent="alfred_private", area=area, finding_id=None,
            ts=f"root-{channel}",
        )
    return routing_with_db


def _add_follow_up(db, *, place, key="k1", status="raised"):
    follow_up_id = db.upsert_follow_up(
        source="slack_shared" if place == "shared" else "claude_code", ref=f"ref-{key}",
        asked_by="owner", place=place, summary=f"item {key}", dedupe_key=key,
        first_seen="2026-10-14T05:00:00",
    )
    db.update_follow_up(follow_up_id, status=status, last_raised="2026-10-16T05:10:00")
    return follow_up_id


def _status_of(db, follow_up_id):
    return db.get_db().execute(
        "SELECT status FROM follow_ups WHERE id=?", (follow_up_id,)
    ).fetchone()[0]


def _answer_under(parent, text="done", *, sender="UOWNER", channel=DIRECT_MESSAGES,
                  from_bot=True, ts="fu-reply-1", root=None):
    msg = {
        "type": "reply", "sender": sender, "text": text, "timestamp": ts,
        "quoted_timestamp": root or f"root-{channel}", "quoted_text": parent, "group_id": channel,
        "channel_type": "im" if channel == DIRECT_MESSAGES else "channel",
    }
    if from_bot is not None:
        msg["quoted_from_bot"] = from_bot
    return msg


def _on_reply(msg):
    asyncio.run(reply_routing.on_reply(msg))


def _answered_by_the_session():
    return reply_routing.route_reply_to_area_agent.call_count == 1


def _logged_at_debug(caplog):
    return [
        record.getMessage() for record in caplog.records
        if record.levelno == logging.DEBUG and "follow-up fast path" in record.getMessage()
    ]


@pytest.mark.parametrize(
    ("text", "status"),
    [("done", "done"), ("Done", "done"), (" dismiss ", "dismissed"), ("DISMISS", "dismissed")],
)
def test_the_owner_closes_a_private_item_from_the_direct_messages_without_a_session(
    places, text, status,
):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(PRIVATE_LINE, text))

    assert _status_of(places, "F-000001") == status
    reply_routing.send_fallback.assert_awaited_once_with(
        "alfred_private", "fu-reply-1", "F-000001 closed"
    )
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_co_owner_closes_a_shared_item_from_the_shared_channel(places):
    _add_follow_up(places, place="shared")

    _on_reply(_answer_under(SHARED_LINE, sender="UCOOWNER", channel=SHARED_CHANNEL))

    assert _status_of(places, "F-000001") == "done"
    reply_routing.send_fallback.assert_awaited_once_with("alfred", "fu-reply-1", "F-000001 closed")
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_the_owner_closes_a_shared_item_from_the_direct_messages(places):
    _add_follow_up(places, place="shared")

    _on_reply(_answer_under(SHARED_LINE, "dismiss"))

    assert _status_of(places, "F-000001") == "dismissed"
    reply_routing.send_fallback.assert_awaited_once()


@pytest.mark.parametrize("sender", ["UCOOWNER", "UOWNER"], ids=["co-owner", "owner"])
def test_nobody_closes_a_private_item_from_the_shared_channel(places, caplog, sender):
    _add_follow_up(places, place="private")
    caplog.set_level(logging.DEBUG, logger="brain-scheduler")

    _on_reply(_answer_under(PRIVATE_LINE, sender=sender, channel=SHARED_CHANNEL))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()
    assert any("F-000001" in line for line in _logged_at_debug(caplog))


@pytest.mark.parametrize("from_bot", [False, None], ids=["typed by a person", "not reported"])
@pytest.mark.parametrize(
    ("place", "line", "sender", "channel"),
    [
        ("private", PRIVATE_LINE, "UOWNER", DIRECT_MESSAGES),
        ("shared", SHARED_LINE, "UCOOWNER", SHARED_CHANNEL),
        ("shared", SHARED_LINE, "UOWNER", DIRECT_MESSAGES),
    ],
    ids=["owner in the direct messages", "co-owner in the shared channel",
         "owner under a shared item"],
)
def test_a_parent_that_a_bot_did_not_post_closes_nothing(
    places, caplog, from_bot, place, line, sender, channel,
):
    _add_follow_up(places, place=place)
    caplog.set_level(logging.DEBUG, logger="brain-scheduler")

    _on_reply(_answer_under(line, sender=sender, channel=channel, from_bot=from_bot))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()
    assert _logged_at_debug(caplog)


def test_a_sender_who_may_not_use_the_shared_place_closes_nothing_there(places, monkeypatch):
    monkeypatch.setattr(
        reply_routing, "_notifier_options",
        lambda: {"authorized_senders": {"UCOOWNER": {"name": "Sam", "areas": ["alfred"]}}},
    )
    _add_follow_up(places, place="shared")

    _on_reply(_answer_under(SHARED_LINE, sender="UCOOWNER", channel=SHARED_CHANNEL))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()


def test_a_channel_with_no_chat_agent_closes_nothing(places, monkeypatch):
    registry = {name: entry for name, entry in FOLLOW_UP_PLACES.items() if name != "alfred"}
    monkeypatch.setattr("src.orchestrator.registry.AGENT_REGISTRY", registry)
    monkeypatch.setattr(reply_routing, "AGENT_REGISTRY", registry)
    _seed_notification(
        places.get_db(), agent="family", area="family", finding_id=None, ts="root-family",
    )
    _add_follow_up(places, place="shared")

    _on_reply(_answer_under(SHARED_LINE, channel=SHARED_CHANNEL, root="root-family"))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()


@pytest.mark.parametrize(
    ("text", "status"),
    [("done F-000001", "done"), ("Dismiss F-000001", "dismissed"), ("  DONE   f-000001  ", "done")],
)
def test_the_owner_names_a_private_item_under_the_bot_summary_and_it_closes(places, text, status):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(SUMMARY, text))

    assert _status_of(places, "F-000001") == status
    reply_routing.send_fallback.assert_awaited_once_with(
        "alfred_private", "fu-reply-1", "F-000001 closed"
    )
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_co_owner_names_a_shared_item_but_cannot_name_a_private_one(places):
    _add_follow_up(places, place="private", key="k1")
    _add_follow_up(places, place="shared", key="k2")

    _on_reply(_answer_under(
        SUMMARY, "done F-000001", sender="UCOOWNER", channel=SHARED_CHANNEL, ts="reply-private",
    ))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()

    _on_reply(_answer_under(
        SUMMARY, "done F-000002", sender="UCOOWNER", channel=SHARED_CHANNEL, ts="reply-shared",
    ))

    assert _status_of(places, "F-000002") == "done"
    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_awaited_once_with("alfred", "reply-shared", "F-000002 closed")


def test_the_owner_names_a_shared_item_from_the_direct_messages(places):
    _add_follow_up(places, place="shared")

    _on_reply(_answer_under(SUMMARY, "dismiss F-000001"))

    assert _status_of(places, "F-000001") == "dismissed"


@pytest.mark.parametrize("sender", ["UCOOWNER", "UOWNER"], ids=["co-owner", "owner"])
def test_nobody_names_a_private_item_from_the_shared_channel(places, caplog, sender):
    _add_follow_up(places, place="private")
    caplog.set_level(logging.DEBUG, logger="brain-scheduler")

    _on_reply(_answer_under(SUMMARY, "done F-000001", sender=sender, channel=SHARED_CHANNEL))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()
    assert any("F-000001" in line for line in _logged_at_debug(caplog))


@pytest.mark.parametrize("from_bot", [False, None], ids=["typed by a person", "not reported"])
@pytest.mark.parametrize(
    ("place", "sender", "channel"),
    [("private", "UOWNER", DIRECT_MESSAGES), ("shared", "UCOOWNER", SHARED_CHANNEL)],
    ids=["owner in the direct messages", "co-owner in the shared channel"],
)
def test_naming_an_item_under_a_parent_that_a_bot_did_not_post_closes_nothing(
    places, caplog, from_bot, place, sender, channel,
):
    _add_follow_up(places, place=place)
    caplog.set_level(logging.DEBUG, logger="brain-scheduler")

    _on_reply(_answer_under(
        SUMMARY, "done F-000001", sender=sender, channel=channel, from_bot=from_bot,
    ))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()
    assert _logged_at_debug(caplog)


def test_done_alone_under_the_summary_still_takes_the_normal_path(places):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(SUMMARY, "done"))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert reply_routing.route_reply_to_area_agent.call_args.args[0] == "done"


def test_a_close_by_the_fast_path_is_journaled_with_who_closed_it(places):
    _add_follow_up(places, place="shared")

    _on_reply(_answer_under(SHARED_LINE, "dismiss", sender="UCOOWNER", channel=SHARED_CHANNEL))

    rows = places.get_db().execute("SELECT actor, action, subject, detail FROM events").fetchall()
    assert rows == [("Sam", "follow_up_closed", "F-000001", "dismissed")]


def test_a_close_stands_and_is_answered_when_its_journal_row_cannot_be_written(places, monkeypatch):
    import src.db as db

    def unwritable(**fields):
        raise OSError("database is locked")

    monkeypatch.setattr(db, "record_event", unwritable)
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(PRIVATE_LINE))

    assert _status_of(places, "F-000001") == "done"
    reply_routing.send_fallback.assert_awaited_once_with(
        "alfred_private", "fu-reply-1", "F-000001 closed"
    )


def test_the_fast_path_reads_and_writes_the_ledger_off_the_event_loop(places, monkeypatch):
    import threading

    import src.db as db

    _add_follow_up(places, place="private")
    loop_thread = threading.get_ident()
    threads = []
    for name in ("get_follow_up", "update_follow_up", "record_event"):
        real = getattr(db, name)

        def spy(*args, _real=real, **kwargs):
            threads.append(threading.get_ident())
            return _real(*args, **kwargs)

        monkeypatch.setattr(db, name, spy)

    _on_reply(_answer_under(PRIVATE_LINE, "done"))

    assert _status_of(places, "F-000001") == "done"
    assert len(threads) >= 3 and loop_thread not in threads


def test_done_with_an_id_closes_the_follow_up_even_in_a_findings_thread(places, monkeypatch):
    from unittest.mock import MagicMock

    marked = MagicMock()
    monkeypatch.setattr("src.db.mark_finding", marked)
    _seed_notification(
        places.get_db(), agent="alfred_private", area="alfred_private", finding_id="f42",
        ts="finding-root",
    )
    _add_follow_up(places, place="private")

    _on_reply(_answer_under("A finding about the sync", "done F-000001", root="finding-root"))

    assert _status_of(places, "F-000001") == "done"
    marked.assert_not_called()
    reply_routing.send_fallback.assert_awaited_once_with(
        "alfred_private", "fu-reply-1", "F-000001 closed"
    )


def test_the_id_in_the_reply_wins_over_the_line_the_thread_starts_with(places):
    _add_follow_up(places, place="private", key="k1")
    _add_follow_up(places, place="private", key="k2")

    _on_reply(_answer_under(PRIVATE_LINE, "done F-000002"))

    assert _status_of(places, "F-000002") == "done"
    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_awaited_once_with(
        "alfred_private", "fu-reply-1", "F-000002 closed"
    )


@pytest.mark.parametrize(
    "text",
    [
        "done F-000001 thanks", "done F-000001 F-000002", "finished F-000001",
        "done: F-000001", "done F-000001.", "F-000001 done",
    ],
)
def test_only_a_keyword_and_one_id_name_an_item(places, text):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(SUMMARY, text))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()


def test_naming_an_id_the_ledger_does_not_know_takes_the_normal_path(places):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(SUMMARY, "done F-000099"))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert reply_routing.route_reply_to_area_agent.call_args.args[0] == "done F-000099"


def test_a_thread_with_two_ids_closes_the_first_and_the_reply_names_the_other(places):
    _add_follow_up(places, place="private", key="k1")
    _add_follow_up(places, place="private", key="k2")
    parent = PRIVATE_LINE + "\nF-000002 Who books the dentist? (asked 15 Oct in Claude Code)"

    _on_reply(_answer_under(parent))

    assert _status_of(places, "F-000001") == "done"
    assert _status_of(places, "F-000002") == "raised"
    reply_routing.send_fallback.assert_awaited_once_with(
        "alfred_private", "fu-reply-1", "F-000001 closed. Not touched: F-000002."
    )
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_an_id_the_ledger_does_not_know_leaves_the_message_to_the_normal_path(places):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under("F-000099 An item that was never added"))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert reply_routing.route_reply_to_area_agent.call_args.args[0] == "done"


@pytest.mark.parametrize(
    ("parent", "text"),
    [
        (PRIVATE_LINE, "done thanks"),
        (PRIVATE_LINE, "dismissed"),
        (PRIVATE_LINE, "not yet"),
        ("Morning: see " + PRIVATE_LINE, "done"),
        ("f-000001 lower case id", "done"),
        ("F-00001 five digits", "done"),
        ("", "done"),
    ],
    ids=["extra words", "other verb", "other answer", "id not first", "lower case",
         "short id", "no parent text"],
)
def test_only_done_or_dismiss_under_a_line_that_starts_with_an_id_is_a_follow_up_reply(
    places, parent, text,
):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(parent, text))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    assert _answered_by_the_session()


def test_a_sender_who_is_not_authorized_cannot_close_an_item(places):
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(PRIVATE_LINE, sender="UNOBODY"))

    assert _status_of(places, "F-000001") == "raised"
    reply_routing.send_fallback.assert_not_awaited()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_paused_fleet_still_closes_an_item(places, monkeypatch):
    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "1")
    _add_follow_up(places, place="private")

    _on_reply(_answer_under(PRIVATE_LINE))

    assert _status_of(places, "F-000001") == "done"
    reply_routing.send_fallback.assert_awaited_once_with(
        "alfred_private", "fu-reply-1", "F-000001 closed"
    )


def test_authorized_image_only_reply_loads_then_dispatches(
    routing_with_db, _owner_env
):
    image = {"type": "image", "data": "aGVsbG8=", "mimeType": "image/jpeg"}
    loader = AsyncMock(return_value=[image])

    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": "",
        "timestamp": "image-only-1", "group_id": "CFAMILY",
        "image_count": 1, "_load_images": loader,
    }))

    loader.assert_awaited_once()
    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == "Please inspect the attached image(s)."
    assert args[1]["images"] == [image]


def test_on_reply_duplicate_event_is_skipped(routing_with_db, _owner_env):
    _seed_notification(routing_with_db.get_db(), agent="discovery", area="career", finding_id="f7", ts="dup-ts")
    msg = {"type": "reaction", "sender": "UOWNER",
           "emoji": "white_check_mark", "target_timestamp": "dup-ts",
           "group_id": "CCAREER"}
    asyncio.run(reply_routing.on_reply(msg))
    asyncio.run(reply_routing.on_reply(msg))
    _owner_env.assert_called_once()


def test_on_reply_approval_reply_resolves_pending_action(routing_with_db, _owner_env):
    from src.brain.actions import queue
    action_id = _propose_for_area("career")
    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": f"yes {action_id}",
        "timestamp": "appr-1", "group_id": "CCAREER",
    }))
    assert queue.get(action_id)["status"] == "approved"
    _owner_env.assert_not_called()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_text_approval_of_a_system_areas_proposal_passes_in_the_private_place(
    routing_with_db, monkeypatch,
):
    from src.brain.actions import queue

    _places_registry(monkeypatch)
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    monkeypatch.setattr(reply_routing, "route_reply_to_area_agent", AsyncMock())
    action_id = _propose_for_area("housekeeping")
    _seed_notification(
        routing_with_db.get_db(), agent="alfred_private", area="housekeeping",
        finding_id=None, ts="dm-root",
    )

    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": f"yes {action_id}",
        "timestamp": "dm-yes", "quoted_timestamp": "dm-root",
        "group_id": "DPRIV", "channel_type": "im",
    }))

    assert queue.get(action_id)["status"] == "approved"
    reply_routing.route_reply_to_area_agent.assert_not_called()


@pytest.mark.parametrize("plain_reply", [False, True])
def test_external_mcp_bot_roots_are_non_conversational_and_cannot_approve(
    routing_with_db, _owner_env, plain_reply
):
    from src.brain.actions import queue

    action_id = _propose_for_area("career")
    _seed_notification(
        routing_with_db.get_db(),
        agent="external-mcp",
        area="career",
        finding_id=None,
        ts="external-mcp-root",
    )
    text = "please continue" if plain_reply else f"yes {action_id}"

    asyncio.run(
        reply_routing.on_reply(
            {
                "type": "reply",
                "sender": "UOWNER",
                "text": text,
                "timestamp": f"external-mcp-reply-{int(plain_reply)}",
                "quoted_timestamp": "external-mcp-root",
                "group_id": "CCAREER",
            }
        )
    )

    assert queue.get(action_id)["status"] == "pending"
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_on_reply_approval_button_resolves_pending_action(routing_with_db, _owner_env):
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456", type=ActionType.CREATE_REMINDER,
        payload={
            "title": "Reviewable reminder",
            "due": "2026-09-01T09:00:00+02:00",
        },
        rationale="user requested it",
    ))
    result = asyncio.run(reply_routing.on_reply({
        "type": "approval", "sender": "UOWNER", "decision": "approved",
        "action_id": "abc123def456", "group_id": "CFAMILY", "timestamp": "button-1",
    }))
    assert result == {"outcome": "approved", "action_id": "abc123def456"}
    assert queue.get("abc123def456")["status"] == "approved"
    assert queue.get("abc123def456")["approval_channel"] == "slack_button"


def test_approval_button_accepts_action_origin_on_shared_channel(
    routing_with_db, _owner_env, monkeypatch
):
    from src.brain.actions import queue

    action_id = _propose_for_area("blind_spot")
    monkeypatch.setattr(
        reply_routing,
        "_areas_from_group",
        lambda _group: frozenset({"assistant", "blind_spot"}),
    )

    result = asyncio.run(reply_routing.on_reply({
        "type": "approval",
        "sender": "UOWNER",
        "decision": "approved",
        "action_id": action_id,
        "group_id": "CASK",
        "timestamp": "shared-channel-button",
    }))

    assert result == {"outcome": "approved", "action_id": action_id}
    assert queue.get(action_id)["status"] == "approved"


def test_on_reply_approval_button_rejects_unmapped_channel(
    routing_with_db, _owner_env
):
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456",
        type=ActionType.CREATE_REMINDER,
        payload={"title": "x"},
        rationale="test",
        origin_agent="family",
        origin_area="family",
    ))
    result = asyncio.run(reply_routing.on_reply({
        "type": "approval",
        "sender": "UOWNER",
        "decision": "approved",
        "action_id": "abc123def456",
        "group_id": "CUNKNOWN",
        "timestamp": "button-unmapped",
    }))

    assert result == {"outcome": "unauthorized", "action_id": "abc123def456"}
    assert queue.get("abc123def456")["status"] == "pending"


def test_on_reply_approval_button_is_idempotent(routing_with_db, _owner_env):
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456", type=ActionType.CREATE_REMINDER,
        payload={"title": "x"}, rationale="test",
    ))
    first = asyncio.run(reply_routing.on_reply({
        "type": "approval", "sender": "UOWNER", "decision": "rejected",
        "action_id": "abc123def456", "group_id": "CFAMILY", "timestamp": "button-2",
    }))
    second = asyncio.run(reply_routing.on_reply({
        "type": "approval", "sender": "UOWNER", "decision": "rejected",
        "action_id": "abc123def456", "group_id": "CFAMILY", "timestamp": "button-3",
    }))
    assert first["outcome"] == "rejected"
    assert second["outcome"] == "already_resolved"


def test_on_reply_approval_button_checks_area_authorization(
    routing_with_db, _owner_env, monkeypatch
):
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456", type=ActionType.CREATE_REMINDER,
        payload={"title": "x"}, rationale="test",
    ))
    monkeypatch.setattr(reply_routing, "sender_identity",
                        lambda _sender: {"name": "co-owner", "areas": ["family"]})
    monkeypatch.setattr(reply_routing, "_infer_area_from_group", lambda _group: "career")
    result = asyncio.run(reply_routing.on_reply({
        "type": "approval", "sender": "UCO", "decision": "approved",
        "action_id": "abc123def456", "group_id": "CCAREER", "timestamp": "button-4",
    }))
    assert result["outcome"] == "unauthorized"
    assert queue.get("abc123def456")["status"] == "pending"


def test_on_reply_approval_button_is_bound_to_action_origin_area(
    routing_with_db, _owner_env, monkeypatch
):
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def456", type=ActionType.CREATE_REMINDER,
        payload={"title": "x"}, rationale="test",
        origin_agent="family", origin_area="family",
    ))
    monkeypatch.setattr(reply_routing, "_infer_area_from_group", lambda _group: "career")
    result = asyncio.run(reply_routing.on_reply({
        "type": "approval", "sender": "UOWNER", "decision": "approved",
        "action_id": "abc123def456", "group_id": "CCAREER",
        "timestamp": "button-cross-area",
    }))
    assert result["outcome"] == "unauthorized"
    assert queue.get("abc123def456")["status"] == "pending"


def test_on_reply_approval_blocked_when_sender_not_authorized_for_area(
    routing_with_db, _owner_env, monkeypatch
):
    from src.brain.actions import queue
    action_id = _propose_for_area("career")

    monkeypatch.setattr(reply_routing, "sender_identity",
                        lambda s: {"name": "co-owner", "areas": ["family"]})
    monkeypatch.setattr(reply_routing, "sender_allowed_for_area",
                        lambda identity, area: False)

    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "Uco", "text": f"yes {action_id}",
        "timestamp": "appr-blocked", "group_id": "CCAREER",
    }))
    assert queue.get(action_id)["status"] == "pending"


def test_area_scoped_sender_can_approve_only_in_physical_area(
    routing_with_db, _owner_env, monkeypatch
):
    from src.brain.actions import queue

    action_id = _propose_for_area("family")
    monkeypatch.setattr(
        reply_routing,
        "sender_identity",
        lambda _sender: {"name": "family member", "areas": ["family"]},
    )
    fallback = AsyncMock()
    monkeypatch.setattr(reply_routing, "send_fallback", fallback)

    asyncio.run(reply_routing.on_reply({
        "type": "reply",
        "sender": "UFAMILY",
        "text": f"yes {action_id}",
        "timestamp": "family-approval",
        "group_id": "CFAMILY",
    }))

    assert queue.get(action_id)["status"] == "approved"
    fallback.assert_not_awaited()
    reply_routing.route_reply_to_area_agent.assert_not_called()


class _ScopeRunner:
    name = "claude"

    def __init__(self):
        self.captured = None

    async def run_reply(self, text, agent_cfg=None, context=None):
        self.captured = {**context, "agent_cfg": agent_cfg}
        return {"status": "success", "response": "", "reply_delivered": True}


@pytest.fixture
def shared_place(routing_with_db, monkeypatch):
    registry = _chat_registry(monkeypatch)
    registry["family"]["vault_paths"] = ["Family/"]
    monkeypatch.setattr(
        "src.brain.runtime.loader.load_all",
        lambda _root: LoadedConfig(repo_root=Path(__file__).resolve().parent.parent),
    )
    monkeypatch.setattr("src.brain.mcp._common.notifier", lambda: object())
    runner = _ScopeRunner()
    monkeypatch.setattr(reply_routing, "ClaudeRunner", lambda: runner)
    monkeypatch.setattr(reply_routing, "send_fallback", AsyncMock())
    return runner


def _area_scoped_reply(monkeypatch, areas, *, quoted_timestamp=None):
    monkeypatch.setattr(
        reply_routing,
        "sender_identity",
        lambda _sender: {"name": "family member", "areas": areas, "is_owner": False},
    )
    started = []
    monkeypatch.setattr(
        reply_routing, "spawn_tracked", lambda coro, _label, _log: started.append(coro)
    )

    async def run():
        await reply_routing.on_reply({
            "type": "reply",
            "sender": "UFAMILY",
            "text": "Alfred, what do you know about the rest of the vault?",
            "timestamp": "family-chat",
            "group_id": "CSHARED",
            "quoted_timestamp": quoted_timestamp,
        })
        for coro in started:
            await coro

    asyncio.run(run())


@pytest.mark.parametrize(
    "areas,quoted_ts,expected_area",
    [
        pytest.param(["alfred", "family"], None, "alfred", id="new-message"),
        pytest.param(["alfred", "family"], "1.1", "family", id="thread-of-an-area"),
    ],
)
def test_area_scoped_sender_is_dispatched_with_the_scope_of_the_shared_place(
    shared_place, routing_with_db, monkeypatch, areas, quoted_ts, expected_area
):
    from src.orchestrator.config import VAULT_PATH

    _seed_notification(
        routing_with_db.get_db(), agent="family", area="family", finding_id=None, ts="1.1"
    )

    _area_scoped_reply(monkeypatch, areas, quoted_timestamp=quoted_ts)

    run = shared_place.captured
    assert run is not None, "the reply was not dispatched"
    assert run["agent_cfg"]["name"] == "alfred"
    assert run["area"] == expected_area
    assert run["areas"] == ["alfred", "family"]
    assert run["read_roots"] is not None
    assert run["read_roots"] == [VAULT_PATH / "Family"]
    reply_routing.send_fallback.assert_not_awaited()


@pytest.mark.parametrize(
    "areas,quoted_ts",
    [
        pytest.param(["family"], None, id="new-message-needs-the-chat-agents-area"),
        pytest.param(["alfred"], "1.1", id="thread-needs-the-threads-area"),
        pytest.param([], None, id="empty-list"),
    ],
)
def test_area_scoped_sender_without_the_authorization_area_is_refused_before_dispatch(
    shared_place, routing_with_db, monkeypatch, areas, quoted_ts
):
    _seed_notification(
        routing_with_db.get_db(), agent="family", area="family", finding_id=None, ts="1.1"
    )

    _area_scoped_reply(monkeypatch, areas, quoted_timestamp=quoted_ts)

    assert shared_place.captured is None
    reply_routing.send_fallback.assert_not_awaited()


def test_area_scoped_sender_covering_only_part_of_the_place_is_refused_by_the_dispatcher(
    shared_place, routing_with_db, monkeypatch
):
    _seed_notification(
        routing_with_db.get_db(), agent="family", area="family", finding_id=None, ts="1.1"
    )

    _area_scoped_reply(monkeypatch, ["family"], quoted_timestamp="1.1")

    assert shared_place.captured is None
    reply_routing.send_fallback.assert_awaited_once_with(
        "family", "family-chat", "This conversation is not available to you."
    )


def test_wildcard_coowner_may_dispatch_conversation(
    routing_with_db, _owner_env, monkeypatch, caplog
):
    monkeypatch.setattr(
        reply_routing,
        "sender_identity",
        lambda _sender: {"name": "co-owner", "areas": "*"},
    )

    caplog.set_level("INFO", logger="brain-scheduler")
    secret_text = "Please investigate token=super-secret-value."
    asyncio.run(reply_routing.on_reply({
        "type": "reply",
        "sender": "UCOOWNER",
        "text": secret_text,
        "timestamp": "coowner-chat",
        "group_id": "CFAMILY",
    }))

    reply_routing.route_reply_to_area_agent.assert_called_once()
    assert secret_text not in caplog.text
    assert "super-secret-value" not in caplog.text


@pytest.fixture
def _paused(monkeypatch):
    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "1")
    fallback = AsyncMock()
    monkeypatch.setattr(reply_routing, "send_fallback", fallback)
    return fallback


def test_paused_reply_gets_fixed_answer_and_no_dispatch(
    routing_with_db, _owner_env, _paused, monkeypatch
):
    loader = AsyncMock()

    asyncio.run(reply_routing.on_reply({
        "type": "reply",
        "sender": "UOWNER",
        "text": "please handle this",
        "timestamp": "paused-dm",
        "group_id": "CCAREER",
        "image_count": 1,
        "_load_images": loader,
    }))

    reply_routing.route_reply_to_area_agent.assert_not_called()
    loader.assert_not_awaited()
    _paused.assert_awaited_once_with(
        "career", "paused-dm", reply_routing.AGENTS_PAUSED_REPLY
    )
    assert reply_routing.AGENTS_PAUSED_REPLY == "Agents are paused. Ask in Claude instead."


def test_paused_reply_delivered_twice_is_answered_once(
    routing_with_db, _owner_env, _paused, monkeypatch
):
    message = {
        "type": "reply",
        "sender": "UOWNER",
        "text": "please handle this",
        "timestamp": "paused-dup",
        "group_id": "CCAREER",
    }

    asyncio.run(reply_routing.on_reply(dict(message)))
    asyncio.run(reply_routing.on_reply(dict(message)))

    _paused.assert_awaited_once()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_paused_keyword_reply_still_marks_finding(routing_with_db, _owner_env, _paused):
    _seed_notification(
        routing_with_db.get_db(),
        agent="discovery", area="career", finding_id="f77", ts="q-ts-paused",
    )

    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": "done",
        "timestamp": "paused-done", "quoted_timestamp": "q-ts-paused",
        "group_id": "CCAREER",
    }))

    _owner_env.assert_called_once()
    assert _owner_env.call_args.args[1] == "f77"
    assert _owner_env.call_args.args[2] == "done"
    _paused.assert_not_awaited()


def test_paused_approval_reply_still_resolves_action(routing_with_db, _owner_env, _paused):
    from src.brain.actions import queue

    action_id = _propose_for_area("career")

    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": f"yes {action_id}",
        "timestamp": "paused-appr", "group_id": "CCAREER",
    }))

    assert queue.get(action_id)["status"] == "approved"
    _paused.assert_not_awaited()


def test_paused_approval_button_still_resolves_action(routing_with_db, _owner_env, _paused):
    from src.brain.actions import queue
    from src.brain.actions.types import Action, ActionType

    queue.insert(Action(
        id="abc123def457", type=ActionType.CREATE_REMINDER,
        payload={
            "title": "Reviewable reminder",
            "due": "2026-09-01T09:00:00+02:00",
        },
        rationale="user requested it",
    ))

    result = asyncio.run(reply_routing.on_reply({
        "type": "approval", "sender": "UOWNER", "decision": "approved",
        "action_id": "abc123def457", "group_id": "CFAMILY",
        "timestamp": "paused-button",
    }))

    assert result == {"outcome": "approved", "action_id": "abc123def457"}
    assert queue.get("abc123def457")["status"] == "approved"
    _paused.assert_not_awaited()


@pytest.mark.parametrize("plain_reply", [False, True])
def test_paused_reply_under_unregistered_caller_gets_fixed_answer_and_cannot_approve(
    routing_with_db, _owner_env, _paused, plain_reply
):
    from src.brain.actions import queue

    action_id = _propose_for_area("career")
    _seed_notification(
        routing_with_db.get_db(),
        agent="external-mcp",
        area="career",
        finding_id=None,
        ts="external-mcp-root",
    )
    text = "please continue" if plain_reply else f"yes {action_id}"
    reply_ts = f"paused-external-mcp-reply-{int(plain_reply)}"

    asyncio.run(
        reply_routing.on_reply(
            {
                "type": "reply",
                "sender": "UOWNER",
                "text": text,
                "timestamp": reply_ts,
                "quoted_timestamp": "external-mcp-root",
                "group_id": "CCAREER",
            }
        )
    )

    assert queue.get(action_id)["status"] == "pending"
    reply_routing.route_reply_to_area_agent.assert_not_called()
    _paused.assert_awaited_once_with("career", reply_ts, reply_routing.AGENTS_PAUSED_REPLY)


WAV = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
VOICE_FALLBACK = "I could not read that voice note. Please type it."
OVER_LIMIT_FALLBACK = "That clip is over 10 minutes or 25 MB; please send a shorter one."
UNREAD_LINE = "[a voice note was attached but could not be read]"


def _wav_header(seconds):
    size = int(seconds * 32000)
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    return b"RIFF" + struct.pack("<I", 36 + size) + b"WAVE" + fmt + b"data" + struct.pack("<I", size)


@pytest.fixture
def _voice(monkeypatch):
    monkeypatch.setenv("BRAIN_WHISPER_MODEL", "/models/ggml-small.bin")
    fallback = AsyncMock()
    monkeypatch.setattr(reply_routing, "send_fallback", fallback)
    heard = SimpleNamespace(fallback=fallback, calls=[], says="what is on today")

    def fake_transcribe(data, *, model, **kwargs):
        heard.calls.append({
            "data": data, "model": model, "kind": kwargs.get("kind"), "thread": threading.current_thread(),
        })
        return heard.says

    monkeypatch.setattr(reply_routing, "transcribe", fake_transcribe)
    return heard


def _voice_message(loader, ts, **fields):
    return {
        "type": "reply", "sender": "UOWNER", "text": "", "timestamp": ts,
        "group_id": "CFAMILY", "_load_voice": loader, **fields,
    }


def test_a_voice_note_is_transcribed_and_dispatched_as_the_senders_message(
    routing_with_db, _owner_env, _voice
):
    loader = AsyncMock(return_value=WAV)
    msg = _voice_message(loader, "voice-ok")

    asyncio.run(reply_routing.on_reply(msg))

    loader.assert_awaited_once()
    assert [(call["data"], call["model"], call["kind"]) for call in _voice.calls] == [
        (WAV, Path("/models/ggml-small.bin"), "wav")
    ]
    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == "[voice note, transcribed] what is on today"
    assert args[1]["text"] == "[voice note, transcribed] what is on today"
    assert "_load_voice" not in args[1]
    _voice.fallback.assert_not_awaited()


def test_the_transcription_runs_off_the_event_loop(routing_with_db, _owner_env, _voice):
    asyncio.run(reply_routing.on_reply(_voice_message(AsyncMock(return_value=WAV), "voice-thread")))

    assert len(_voice.calls) == 1
    assert _voice.calls[0]["thread"] is not threading.main_thread()


def test_a_spoken_keyword_is_not_a_keyword_reply(routing_with_db, _owner_env, _voice):
    _seed_notification(
        routing_with_db.get_db(), agent="discovery", area="career", finding_id="f31", ts="q-voice",
    )
    _voice.says = "done"

    asyncio.run(reply_routing.on_reply(_voice_message(
        AsyncMock(return_value=WAV), "voice-done", group_id="CCAREER", quoted_timestamp="q-voice",
    )))

    _owner_env.assert_not_called()
    assert reply_routing.route_reply_to_area_agent.call_args.args[0] == "[voice note, transcribed] done"


def test_a_typed_keyword_still_wins_before_any_download(routing_with_db, _owner_env, _voice):
    _seed_notification(
        routing_with_db.get_db(), agent="discovery", area="career", finding_id="f32", ts="q-typed",
    )
    loader = AsyncMock(return_value=WAV)

    asyncio.run(reply_routing.on_reply(_voice_message(
        loader, "typed-done", text="done", group_id="CCAREER", quoted_timestamp="q-typed",
    )))

    loader.assert_not_awaited()
    assert _owner_env.call_args.args[1:] == ("f32", "done")
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_clip_whose_bytes_are_not_audio_gets_the_fallback_and_is_never_converted(
    routing_with_db, _owner_env, _voice
):
    for ts, body in (("voice-png", PNG), ("voice-zip", b"PK\x03\x04" + b"\x00" * 16)):
        asyncio.run(reply_routing.on_reply(_voice_message(AsyncMock(return_value=body), ts)))

        _voice.fallback.assert_awaited_with("family", ts, VOICE_FALLBACK)

    assert _voice.fallback.await_count == 2
    assert _voice.calls == []
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_an_image_behind_an_id3_tag_is_read_only_as_mp3_and_gets_the_fallback(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    import functools

    from src.brain.media import voice

    commands = []

    def ffmpeg(args, **kwargs):
        commands.append(list(args))
        return SimpleNamespace(
            returncode=183, stdout=b"", stderr=b"Failed to find two consecutive MPEG audio frames.",
        )

    monkeypatch.setattr(reply_routing, "transcribe", functools.partial(voice.transcribe, run=ffmpeg))

    asyncio.run(reply_routing.on_reply(
        _voice_message(AsyncMock(return_value=b"ID3\x03" + 6 * b"\x00" + PNG), "voice-id3-png")
    ))

    [convert] = commands
    i = convert.index("-i")
    assert convert[i - 2:i] == ["-f", "mp3"]
    _voice.fallback.assert_awaited_once_with("family", "voice-id3-png", VOICE_FALLBACK)
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_whisper_timeout_costs_one_fallback_reply_and_leaves_no_folder(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    import functools
    import subprocess

    from src.brain.media import voice

    folders, stages = [], []

    def run(args, **kwargs):
        if "-nostdin" in args:
            stages.append("ffmpeg")
            folders.append(Path(args[-1]).parent)
            Path(args[-1]).write_bytes(_wav_header(5))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        stages.append("whisper")
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(reply_routing, "transcribe", functools.partial(voice.transcribe, run=run))

    asyncio.run(reply_routing.on_reply(_voice_message(AsyncMock(return_value=WAV), "voice-hang")))

    _voice.fallback.assert_awaited_once_with("family", "voice-hang", VOICE_FALLBACK)
    assert stages == ["ffmpeg", "whisper"]
    assert len(folders) == 1 and not folders[0].exists()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_clip_that_decodes_to_more_than_ten_minutes_gets_the_over_limit_reply(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    import functools

    from src.brain.media import voice

    stages = []

    def run(args, **kwargs):
        stages.append(args[0])
        if "-nostdin" in args:
            Path(args[-1]).write_bytes(_wav_header(601))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError("whisper-cli ran on a clip over the limit")

    monkeypatch.setattr(reply_routing, "transcribe", functools.partial(voice.transcribe, run=run))

    asyncio.run(reply_routing.on_reply(_voice_message(AsyncMock(return_value=WAV), "voice-601")))

    assert stages == ["ffmpeg"]
    _voice.fallback.assert_awaited_once_with("family", "voice-601", OVER_LIMIT_FALLBACK)
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_voice_notes_are_downloaded_and_transcribed_one_at_a_time(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    async def download():
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.05)
        return WAV

    def slow(data, *, model, **_kwargs):
        time.sleep(0.2)
        with lock:
            state["now"] -= 1
        return "hi"

    monkeypatch.setattr(reply_routing, "transcribe", slow)

    async def three():
        await asyncio.gather(*(
            reply_routing.on_reply(_voice_message(download, f"voice-{i}")) for i in range(3)
        ))

    asyncio.run(three())

    assert state == {"now": 0, "peak": 1}
    assert reply_routing.route_reply_to_area_agent.call_count == 3


@pytest.mark.parametrize("loader", [
    AsyncMock(return_value=None),
    AsyncMock(return_value=b""),
    AsyncMock(side_effect=RuntimeError("download broke")),
], ids=["no-bytes", "empty-bytes", "download-error"])
def test_a_clip_that_cannot_be_fetched_gets_the_fallback(routing_with_db, _owner_env, _voice, loader):
    asyncio.run(reply_routing.on_reply(_voice_message(loader, "voice-lost")))

    _voice.fallback.assert_awaited_once_with("family", "voice-lost", VOICE_FALLBACK)
    assert _voice.calls == []
    reply_routing.route_reply_to_area_agent.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_without_a_model_a_voice_note_gets_the_fallback(
    routing_with_db, _owner_env, _voice, monkeypatch, value
):
    if value is None:
        monkeypatch.delenv("BRAIN_WHISPER_MODEL")
    else:
        monkeypatch.setenv("BRAIN_WHISPER_MODEL", value)

    asyncio.run(reply_routing.on_reply(_voice_message(AsyncMock(return_value=WAV), "voice-nomodel")))

    _voice.fallback.assert_awaited_once_with("family", "voice-nomodel", VOICE_FALLBACK)
    assert _voice.calls == []
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_paused_fleet_never_calls_the_voice_loader(routing_with_db, _owner_env, _voice, monkeypatch):
    monkeypatch.setenv("BRAIN_AGENTS_PAUSED", "1")
    loader = AsyncMock(return_value=WAV)

    asyncio.run(reply_routing.on_reply(_voice_message(loader, "voice-paused")))

    loader.assert_not_awaited()
    assert _voice.calls == []
    _voice.fallback.assert_awaited_once_with("family", "voice-paused", reply_routing.AGENTS_PAUSED_REPLY)
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_stranger_never_reaches_the_voice_loader(routing_with_db, _owner_env, _voice):
    loader = AsyncMock(return_value=WAV)

    asyncio.run(reply_routing.on_reply(_voice_message(loader, "voice-stranger", sender="UNOBODY")))

    loader.assert_not_awaited()
    assert _voice.calls == []
    _voice.fallback.assert_not_awaited()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_non_owner_direct_message_is_refused_before_the_voice_loader(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    monkeypatch.setattr(
        reply_routing, "sender_identity",
        lambda _sender: {"name": "co-owner", "areas": "*", "is_owner": False},
    )
    send_direct = AsyncMock()
    monkeypatch.setattr(reply_routing, "send_direct", send_direct)
    loader = AsyncMock(return_value=WAV)

    asyncio.run(reply_routing.on_reply(_voice_message(
        loader, "voice-dm", sender="UCOOWNER", group_id="DCOOWNER", channel_type="im",
    )))

    send_direct.assert_awaited_once_with("DCOOWNER", "voice-dm", "Please write in the shared channel.")
    loader.assert_not_awaited()
    assert _voice.calls == []
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_reply_with_no_text_no_image_and_no_voice_note_is_still_ignored(
    routing_with_db, _owner_env, _voice
):
    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": "  ", "timestamp": "empty-1",
        "group_id": "CFAMILY", "_load_voice": None,
    }))

    _voice.fallback.assert_not_awaited()
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_a_typed_caption_stays_above_the_transcript(routing_with_db, _owner_env, _voice):
    msg = _voice_message(AsyncMock(return_value=WAV), "voice-caption", text="  summarise this  ")

    asyncio.run(reply_routing.on_reply(msg))

    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == "summarise this\n[voice note, transcribed] what is on today"
    assert args[1]["text"] == args[0]


def test_a_blank_caption_adds_nothing_to_the_transcript(routing_with_db, _owner_env, _voice):
    asyncio.run(reply_routing.on_reply(
        _voice_message(AsyncMock(return_value=WAV), "voice-blank", text=" \n ")
    ))

    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == args[1]["text"] == "[voice note, transcribed] what is on today"


def test_a_caption_survives_a_clip_that_cannot_be_read(routing_with_db, _owner_env, _voice):
    asyncio.run(reply_routing.on_reply(
        _voice_message(AsyncMock(return_value=PNG), "voice-caption-png", text="summarise this")
    ))

    _voice.fallback.assert_not_awaited()
    assert _voice.calls == []
    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == args[1]["text"] == f"summarise this\n{UNREAD_LINE}"


def test_a_caption_survives_a_clip_over_the_limits(routing_with_db, _owner_env, _voice):
    loader = AsyncMock(side_effect=ClipOverLimit("more than 25 MiB"))

    asyncio.run(reply_routing.on_reply(_voice_message(loader, "voice-caption-lost", text="summarise this")))

    _voice.fallback.assert_not_awaited()
    assert _voice.calls == []
    assert reply_routing.route_reply_to_area_agent.call_args.args[0] == f"summarise this\n{UNREAD_LINE}"


def test_a_clip_over_the_limits_gets_the_over_limit_reply_and_nothing_runs(
    routing_with_db, _owner_env, _voice
):
    loader = AsyncMock(side_effect=ClipOverLimit("more than 25 MiB"))

    asyncio.run(reply_routing.on_reply(_voice_message(loader, "voice-long", text=" ")))

    loader.assert_awaited_once()
    _voice.fallback.assert_awaited_once_with("family", "voice-long", OVER_LIMIT_FALLBACK)
    assert _voice.calls == []
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_an_attached_audio_file_is_outside_content_with_its_own_prefix(routing_with_db, _owner_env, _voice):
    asyncio.run(reply_routing.on_reply(_voice_message(
        AsyncMock(return_value=WAV), "voice-attached", text="what does she say?", voice_attached=True,
    )))

    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == args[1]["text"] == "what does she say?\n[attached audio, transcribed] what is on today"
    assert args[1]["outside_content"] is True


def test_a_clip_recorded_in_slack_stays_the_senders_own_words(routing_with_db, _owner_env, _voice):
    asyncio.run(reply_routing.on_reply(_voice_message(
        AsyncMock(return_value=WAV), "voice-recorded", voice_attached=False,
    )))

    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == "[voice note, transcribed] what is on today"
    assert "outside_content" not in args[1]


def test_an_attached_audio_file_that_cannot_be_read_brings_no_outside_content(
    routing_with_db, _owner_env, _voice
):
    asyncio.run(reply_routing.on_reply(_voice_message(
        AsyncMock(return_value=PNG), "voice-attached-png", text="what does she say?", voice_attached=True,
    )))

    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == f"what does she say?\n{UNREAD_LINE}"
    assert "outside_content" not in args[1]


def _slack_clip_event(text="", **file_fields):
    return {
        "user": "UOWNER", "text": text, "channel": "CFAMILY", "channel_type": "channel", "ts": "900.1",
        "files": [{
            "subtype": "slack_audio", "mimetype": "audio/webm", "url_private": "https://files.slack.com/clip",
            "user": "UOWNER", **file_fields,
        }],
    }


def _from_the_listener(event):
    from src.brain.plugins.listeners.slack import _normalize_event

    return asyncio.run(_normalize_event(event, "message", web_client=SimpleNamespace(token="xoxb-secret")))


def test_an_oversize_clip_with_no_text_gets_the_over_limit_reply_from_the_listener_on(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    monkeypatch.delenv("BRAIN_MEDIA", raising=False)
    msg = _from_the_listener(_slack_clip_event(size=30 * 1024 * 1024))

    asyncio.run(reply_routing.on_reply(msg))

    _voice.fallback.assert_awaited_once_with("family", "900.1", OVER_LIMIT_FALLBACK)
    assert _voice.calls == []
    reply_routing.route_reply_to_area_agent.assert_not_called()


def test_an_audio_file_attached_in_slack_reaches_the_run_as_outside_content(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    monkeypatch.delenv("BRAIN_MEDIA", raising=False)
    msg = _from_the_listener(_slack_clip_event(subtype=None, mimetype="audio/mp4"))
    assert callable(msg["_load_voice"])
    msg["_load_voice"] = AsyncMock(return_value=WAV)

    asyncio.run(reply_routing.on_reply(msg))

    args = reply_routing.route_reply_to_area_agent.call_args.args
    assert args[0] == "[attached audio, transcribed] what is on today"
    assert args[1]["outside_content"] is True


def test_a_question_with_an_attached_screen_recording_is_answered_as_before(
    routing_with_db, _owner_env, _voice, monkeypatch
):
    monkeypatch.delenv("BRAIN_MEDIA", raising=False)
    msg = _from_the_listener(_slack_clip_event(
        text="why does this fail?", subtype=None, mimetype="video/quicktime", size=90 * 1024 * 1024,
    ))

    asyncio.run(reply_routing.on_reply(msg))

    _voice.fallback.assert_not_awaited()
    assert _voice.calls == []
    assert reply_routing.route_reply_to_area_agent.call_args.args[0] == "why does this fail?"


@pytest.mark.parametrize("fields", [{"size": 30 * 1024 * 1024}, {"size": 1000}])
def test_with_the_media_switch_off_a_clip_is_ignored_as_before(
    routing_with_db, _owner_env, _voice, monkeypatch, fields
):
    monkeypatch.setenv("BRAIN_MEDIA", "0")
    msg = _from_the_listener(_slack_clip_event(**fields))

    asyncio.run(reply_routing.on_reply(msg))

    _voice.fallback.assert_not_awaited()
    assert _voice.calls == []
    reply_routing.route_reply_to_area_agent.assert_not_called()


def _shared_message(routing_with_db, monkeypatch, **fields):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    _chat_registry(monkeypatch)
    monkeypatch.setattr(reply_routing, "route_reply_to_area_agent", AsyncMock())
    monkeypatch.setattr(reply_routing, "_bot_user_id", AsyncMock(return_value="UBOT"))
    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "group_id": "CSHARED",
        "timestamp": fields.pop("timestamp", "9.1"), **fields,
    }))
    return reply_routing.route_reply_to_area_agent.called


@pytest.mark.parametrize("text", ["<@UPARTNER> hello", "see you at 5", "ok"])
def test_chat_between_people_in_the_shared_channel_gets_no_reply(routing_with_db, monkeypatch, text):
    assert not _shared_message(routing_with_db, monkeypatch, text=text)


@pytest.mark.parametrize("text", ["Alfred, what is on this weekend?", "what's on tomorrow alfred",
                                  "<@UBOT> any plans?"])
def test_a_message_that_addresses_alfred_is_answered(routing_with_db, monkeypatch, text):
    assert _shared_message(routing_with_db, monkeypatch, text=text)


def test_a_reply_in_a_thread_alfred_started_is_answered(routing_with_db, monkeypatch):
    assert _shared_message(routing_with_db, monkeypatch, text="and Sunday?",
                           quoted_timestamp="7.7", quoted_from_bot=True)


def test_a_reply_in_a_thread_that_began_by_asking_alfred_is_answered(routing_with_db, monkeypatch):
    assert _shared_message(routing_with_db, monkeypatch, text="and Sunday?",
                           quoted_timestamp="7.8", quoted_text="Alfred, what's on Saturday?")


def test_a_reply_in_a_thread_between_people_gets_no_reply(routing_with_db, monkeypatch):
    assert not _shared_message(routing_with_db, monkeypatch, text="sounds good",
                               quoted_timestamp="7.9", quoted_text="<@UPARTNER> dinner at 7?")


def test_the_private_channel_still_answers_everything(routing_with_db, monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")
    _chat_registry(monkeypatch)["alfred_private"] = {"chat": "private", "delivery": {"slack": {"channel_id": "DPRIV"}}}
    monkeypatch.setattr(reply_routing, "route_reply_to_area_agent", AsyncMock())
    monkeypatch.setattr(reply_routing, "_bot_user_id", AsyncMock(return_value="UBOT"))
    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "group_id": "DPRIV", "timestamp": "9.2", "text": "ok",
    }))
    assert reply_routing.route_reply_to_area_agent.called


class _TextRunner:
    name = "claude"

    def __init__(self, conn):
        self.conn = conn

    async def run_reply(self, text, agent_cfg=None, context=None):
        _insert_reply_row(self.conn, agent="alfred", group_id="CSHARED", created_at="2999-01-01T00:00:00")
        return {"status": "success", "response": "Sunny, 14 degrees.", "reply_delivered": False}


def test_an_answer_is_posted_even_when_another_reply_landed_in_the_channel_meanwhile(
    shared_place, routing_with_db, monkeypatch,
):
    sent = []

    class Notifier:
        async def send(self, message, **kwargs):
            sent.append((message, kwargs.get("quote_timestamp")))

    monkeypatch.setattr(reply_routing, "ClaudeRunner", lambda: _TextRunner(routing_with_db.get_db()))
    monkeypatch.setattr("src.brain.mcp._common.notifier", lambda: Notifier())
    _area_scoped_reply(monkeypatch, ["alfred", "family"])

    assert sent == [("Sunny, 14 degrees.", "family-chat")]


def test_a_thread_started_by_the_owners_routine_is_answered_by_the_channel_chat(
    shared_place, routing_with_db, monkeypatch,
):
    _seed_notification(
        routing_with_db.get_db(), agent="external-mcp", area="family", finding_id=None, ts="1.1"
    )
    routing_with_db.get_db().execute("UPDATE notifications SET group_id='CSHARED' WHERE agent='external-mcp'")
    routing_with_db.get_db().commit()

    _area_scoped_reply(monkeypatch, ["alfred", "family"], quoted_timestamp="1.1")

    run = shared_place.captured
    assert run is not None, "the reply was refused"
    assert (run["agent_cfg"]["name"], run["area"]) == ("alfred", "family")


def test_a_refused_thread_says_so_instead_of_staying_silent(routing_with_db, monkeypatch):
    sent = []

    async def fake_direct(channel, ts, text):
        sent.append((channel, ts, text))

    _chat_registry(monkeypatch)
    monkeypatch.setattr(reply_routing, "send_direct", fake_direct)
    monkeypatch.setattr(reply_routing, "resolve_reply_context",
                        lambda msg: {"agent": "nobody", "area": "career", "finding_id": None})
    monkeypatch.setattr(reply_routing, "sender_identity",
                        lambda _s: {"name": "owner", "areas": "*", "is_owner": True})
    monkeypatch.setattr(reply_routing, "_addressed_to_alfred", AsyncMock(return_value=True))
    asyncio.run(reply_routing.on_reply({
        "type": "reply", "sender": "UOWNER", "text": "Alfred, hello", "timestamp": "9.9", "group_id": "CSHARED",
    }))
    assert sent == [("CSHARED", "9.9", reply_routing.THREAD_NOT_ANSWERABLE)]
