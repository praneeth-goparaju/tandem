from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture
def occ(monkeypatch, tmp_path):
    monkeypatch.setenv("OCCASIONS_DIR", str(tmp_path / "occasions"))
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    import src.orchestrator.config as cfg

    importlib.reload(cfg)
    import src.brain.occasions.store as store

    importlib.reload(store)
    store.add_person("Ravi Kumar", e164="+10000000000")
    yield store
    importlib.reload(cfg)


def _run(c):
    from src.brain.mcp.caller_context import ToolCaller, caller_scope

    with caller_scope(ToolCaller(agent="humans", area="humans", is_reply=True)):
        return asyncio.run(c)


def _text(r):
    return r["content"][0]["text"]


def test_update_person_tier_inscope_cadence(occ):
    from src.brain.mcp.tools.humans import humans_update_person

    _run(humans_update_person({"person_id": "ravi-kumar", "tier": "inner", "in_scope": True, "cadence": "monthly"}))
    p = occ.people_by_id()["ravi-kumar"]
    assert p.tier == "inner" and p.in_scope is True and p.cadence == "monthly"
    _run(humans_update_person({"person_id": "ravi-kumar", "cadence": "off"}))
    assert occ.people_by_id()["ravi-kumar"].cadence == ""


def test_set_birthday_and_festival(occ):
    from src.brain.mcp.tools.humans import humans_set_occasion

    _run(humans_set_occasion({"person_id": "ravi-kumar", "occasion": "birthday", "date": "--03-03"}))
    p = occ.people_by_id()["ravi-kumar"]
    assert p.occasions.birthday.enabled is True
    assert p.occasions.birthday.date == "--03-03"

    _run(humans_set_occasion({"person_id": "ravi-kumar", "occasion": "diwali"}))
    assert "diwali" in occ.people_by_id()["ravi-kumar"].occasions.festivals
    _run(humans_set_occasion({"person_id": "ravi-kumar", "occasion": "diwali", "enabled": False}))
    assert "diwali" not in occ.people_by_id()["ravi-kumar"].occasions.festivals


def test_set_occasion_warns_when_not_in_scope(occ):
    from src.brain.mcp.tools.humans import humans_set_occasion

    out = _text(_run(humans_set_occasion({"person_id": "ravi-kumar", "occasion": "birthday", "date": "--03-03"})))
    assert "not in_scope" in out


def test_update_unknown_person(occ):
    from src.brain.mcp.tools.humans import humans_update_person

    assert "ERROR" in _text(_run(humans_update_person({"person_id": "ghost", "tier": "inner"})))
