from __future__ import annotations

import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def occ(monkeypatch, tmp_path):
    monkeypatch.setenv("OCCASIONS_DIR", str(tmp_path / "occasions"))
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault"))
    import src.orchestrator.config as cfg

    importlib.reload(cfg)
    import src.brain.occasions.store as store

    importlib.reload(store)
    import src.brain.occasions.diary as diary

    importlib.reload(diary)
    yield store, diary
    importlib.reload(cfg)


def test_stay_in_touch_due(occ):
    store, _ = occ
    from src.brain.occasions.models import PeopleFile, Person

    today = date(2026, 6, 18)
    store.save_people(
        PeopleFile(
            people=[
                Person(id="overdue", name="Overdue", tier="close", last_interaction=(today - timedelta(days=120)).isoformat()),
                Person(id="recent", name="Recent", tier="inner", last_interaction=(today - timedelta(days=10)).isoformat()),
                Person(id="ext", name="Ext", tier="extended", last_interaction=(today - timedelta(days=400)).isoformat()),
                Person(id="fresh", name="Fresh", tier="inner", last_interaction=None),
            ]
        )
    )
    due = {d["person_id"] for d in store.stay_in_touch_due(today)}
    assert due == {"overdue"}


def test_open_threads_list_and_resolve(occ):
    store, diary = occ
    store.add_person("Ravi Kumar")
    diary.append_entry("ravi-kumar", "job interview next week", section="Open threads", when="2026-06-18")
    diary.append_entry("ravi-kumar", "lend him the drill", section="Open threads", when="2026-06-18")

    threads = diary.list_open_threads()
    assert len(threads) == 2
    assert all(t["person_id"] == "ravi-kumar" for t in threads)
    assert all(len(t["thread_id"]) == 12 for t in threads)

    interview_id = next(
        t["thread_id"] for t in threads if "interview" in t["thread"]
    )
    assert diary.resolve_thread(
        "ravi-kumar", thread_id=interview_id, outcome="got the job!",
    ) is True
    remaining = [t["thread"] for t in diary.list_open_threads()]
    assert not any("interview" in t for t in remaining)
    assert any("drill" in t for t in remaining)

    import src.orchestrator.config as cfg

    md = (cfg.VAULT_PATH / "12 - People/Ravi Kumar.md").read_text()
    assert "got the job!" in md
    assert "## Resolved threads" in md
    assert f"[{interview_id}]" in md
    assert "job interview next week" in md


def test_resolve_nonexistent_thread(occ):
    store, diary = occ
    store.add_person("Ravi Kumar")
    assert diary.resolve_thread("ravi-kumar", "nothing-here") is False


def test_checked_threads_are_not_listed(occ):
    store, diary = occ
    store.add_person("Ravi Kumar")
    diary.append_entry(
        "ravi-kumar", "[x] already handled", section="Open threads",
        when="2026-06-18",
    )

    assert diary.list_open_threads() == []


def test_legacy_substring_refuses_ambiguous_match_without_writing(occ):
    store, diary = occ
    store.add_person("Ravi Kumar")
    diary.append_entry(
        "ravi-kumar", "ask about interview result", section="Open threads",
        when="2026-06-18",
    )
    diary.append_entry(
        "ravi-kumar", "ask about interview timing", section="Open threads",
        when="2026-06-19",
    )
    import src.orchestrator.config as cfg

    path = cfg.VAULT_PATH / "12 - People/Ravi Kumar.md"
    before = path.read_text()
    with pytest.raises(ValueError, match="ambiguous"):
        diary.resolve_thread("ravi-kumar", "interview")
    assert path.read_text() == before
