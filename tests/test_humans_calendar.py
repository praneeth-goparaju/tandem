from __future__ import annotations

import asyncio
import importlib
import json

import pytest


@pytest.fixture
def occ(monkeypatch, tmp_path):
    monkeypatch.setenv("OCCASIONS_DIR", str(tmp_path / "occasions"))
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    import src.orchestrator.config as cfg

    importlib.reload(cfg)
    import src.brain.occasions.store as store

    importlib.reload(store)
    import src.brain.occasions.calendar_people as cp

    importlib.reload(cp)
    yield store, cp
    importlib.reload(cfg)


def test_match_people_in_events(occ):
    store, cp = occ
    from src.brain.occasions.models import Person

    people = [
        Person(id="ravi-kumar", name="Ravi Kumar"),
        Person(id="anjali-rao", name="Anjali Rao"),
        Person(id="bo", name="Bo"),
    ]
    events = [
        {"summary": "Lunch with Ravi", "start": "2026-06-18T13:00", "end": "2026-06-18T14:00"},
        {"summary": "Dentist", "start": "", "end": ""},
        {"summary": "Call Anjali Rao re project", "start": "", "end": ""},
        {"summary": "bored meeting", "start": "", "end": ""},
    ]
    matched = cp.match_people_in_events(events, people)
    got = {(m["summary"], tuple(x["person_id"] for x in m["matched"])) for m in matched}
    assert ("Lunch with Ravi", ("ravi-kumar",)) in got
    assert ("Call Anjali Rao re project", ("anjali-rao",)) in got
    assert len(matched) == 2


def test_meetings_tool_with_stubbed_calendar(occ, monkeypatch):
    store, cp = occ
    from src.brain.occasions.models import PeopleFile, Person

    store.save_people(PeopleFile(people=[Person(id="ravi-kumar", name="Ravi Kumar")]))
    monkeypatch.setattr(
        cp, "_fetch_events",
        lambda d_from, d_to: [{"summary": "Coffee w/ Ravi", "start": "x", "end": "y"}],
    )
    from src.brain.mcp.tools.humans import humans_meetings

    out = json.loads(asyncio.run(humans_meetings({"scope": "today"}))["content"][0]["text"])
    assert out["scope"] == "today"
    assert out["meetings"][0]["summary"] == "[Coffee w/ Ravi]"
    assert out["meetings"][0]["matched"][0]["person_id"] == "ravi-kumar"


def test_no_icloud_returns_empty(occ, monkeypatch):
    from src.brain.mcp.tools import _icloud

    _, cp = occ

    def not_configured():
        raise _icloud.ICloudNotConfigured("no iCloud credentials")

    monkeypatch.setattr(_icloud, "caldav_client", not_configured)
    assert cp.meetings("yesterday") == []
