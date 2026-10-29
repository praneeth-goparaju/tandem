from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta


def _call(**args):
    from src.brain.mcp.tools.ops import ops_summary

    return asyncio.run(ops_summary(args))


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _seed(conn):
    from src.db import insert_agent_run

    now = datetime.now(UTC)
    recent = _iso(now - timedelta(hours=1))
    old = _iso(now - timedelta(hours=48))

    insert_agent_run(conn, agent="health", provider="pi", model="g",
                     status="success", started_at=recent)
    insert_agent_run(conn, agent="career", provider="pi", model="g",
                     status="failed", started_at=recent,
                     error="agy returned empty response (failed_empty)")
    insert_agent_run(conn, agent="family", provider="pi", model="g",
                     status="timeout", started_at=recent,
                     error="agy CLI exceeded 300s")
    insert_agent_run(conn, agent="career", provider="pi", model="g",
                     status="failed", started_at=old,
                     error="stale failure outside window")

    def _pa(id_, type_, status, created, executed=None):
        conn.execute(
            "INSERT INTO pending_actions (id, type, payload_json, dedupe_key, "
            "rationale, risk, status, created_at, executed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (id_, type_, "{}", id_.ljust(64, "0"), "r", "confirm", status, created, executed),
        )

    _pa("p1", "edit_vault_file", "pending", recent)
    _pa("p2", "send_draft", "pending", recent)
    _pa("p3", "promote_note", "executed", recent, executed=recent)
    _pa("p4", "promote_note", "executed", old, executed=old)

    def _ev(ts, action):
        conn.execute(
            "INSERT INTO events (ts, actor, action) VALUES (?,?,?)",
            (ts, "assistant", action),
        )

    _ev(recent, "note_promoted")
    _ev(recent, "brief_published")
    _ev(recent, "note_promoted")
    _ev(old, "intake_dismissed")
    conn.commit()


def test_ops_summary_shapes(isolated_db):
    _seed(isolated_db.get_db())
    payload = _payload(_call(since_hours=24))

    assert payload["since_hours"] == 24

    ar = payload["agent_runs"]
    assert ar["by_status"] == {"success": 1, "failed": 1, "timeout": 1}
    assert ar["total"] == 3
    fails = {f["agent"]: f for f in ar["failures"]}
    assert set(fails) == {"career", "family"}
    assert "failed_empty" in fails["career"]["detail"]
    assert fails["family"]["status"] == "timeout"

    assert payload["pending_actions"] == 2
    assert payload["notes_promoted"] == 1

    ev = payload["events"]
    assert ev["by_action"] == {"note_promoted": 2, "brief_published": 1}
    assert ev["total"] == 3


def test_ops_summary_defaults_to_24h_and_empty_shape(isolated_db):
    payload = _payload(_call())
    assert payload["since_hours"] == 24
    assert payload["agent_runs"] == {
        "total": 0,
        "by_status": {},
        "failures": [],
        "superseded": 0,
    }
    assert payload["pending_actions"] == 0
    assert payload["notes_promoted"] == 0
    assert payload["events"] == {"total": 0, "by_action": {}}


def _run(conn, agent, status, hours_ago, error=""):
    from src.db import insert_agent_run

    insert_agent_run(
        conn,
        agent=agent,
        provider="codex",
        model="gpt-5",
        status=status,
        started_at=_iso(datetime.now(UTC) - timedelta(hours=hours_ago)),
        error=error,
    )
    conn.commit()


def test_failure_superseded_by_a_later_success_is_not_a_live_failure(isolated_db):
    conn = isolated_db.get_db()
    _run(conn, "health", "failed", 3, error="Codex error: The usage limit has been reached")
    _run(conn, "health", "success", 1)

    ar = _payload(_call(since_hours=24))["agent_runs"]

    assert ar["failures"] == []
    assert ar["by_status"] == {"success": 1}
    assert ar["total"] == 1
    assert ar["superseded"] == 1


def test_timeout_superseded_by_a_later_success_is_not_a_live_failure(isolated_db):
    conn = isolated_db.get_db()
    _run(conn, "family", "timeout", 3, error="codex CLI exceeded 300s")
    _run(conn, "family", "success", 1)

    ar = _payload(_call(since_hours=24))["agent_runs"]

    assert ar["failures"] == []
    assert ar["by_status"] == {"success": 1}
    assert ar["superseded"] == 1


def test_failure_after_the_last_success_is_still_reported(isolated_db):
    conn = isolated_db.get_db()
    _run(conn, "career", "success", 3)
    _run(conn, "career", "failed", 1, error="still broken")

    ar = _payload(_call(since_hours=24))["agent_runs"]

    assert [f["agent"] for f in ar["failures"]] == ["career"]
    assert ar["by_status"] == {"success": 1, "failed": 1}
    assert ar["superseded"] == 0


def test_supersession_does_not_cross_agents(isolated_db):
    conn = isolated_db.get_db()
    _run(conn, "health", "failed", 3, error="Codex error: The usage limit has been reached")
    _run(conn, "career", "success", 1)

    ar = _payload(_call(since_hours=24))["agent_runs"]

    assert [f["agent"] for f in ar["failures"]] == ["health"]
    assert ar["by_status"] == {"success": 1, "failed": 1}
    assert ar["superseded"] == 0
