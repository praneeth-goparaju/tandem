from __future__ import annotations

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
    import src.brain.occasions.diary as diary

    importlib.reload(diary)
    yield store, diary
    importlib.reload(cfg)


def test_effective_cadence_defaults_and_override(occ):
    store, _ = occ
    from src.brain.occasions.models import Person

    assert store.effective_cadence(Person(id="a", name="A", tier="inner")) == "monthly"
    assert store.effective_cadence(Person(id="b", name="B", tier="close")) == "quarterly"
    assert store.effective_cadence(Person(id="c", name="C", tier="extended")) is None
    assert store.effective_cadence(Person(id="d", name="D", tier="inner", cadence="weekly")) == "weekly"
    assert store.effective_cadence(Person(id="e", name="E", tier="inner", cadence="")) is None


def test_add_person_is_diary_contact_not_messaging(occ):
    store, _ = occ
    p = store.add_person("Ravi Kumar", e164="+10000000000")
    assert p.id == "ravi-kumar"
    assert p.in_scope is False
    p2 = store.add_person("Ravi Kumar")
    assert p2.id == "ravi-kumar-2"
    assert {x.id for x in store.load_people().people} == {"ravi-kumar", "ravi-kumar-2"}


def test_append_timeline_creates_note_and_bumps_interaction(occ):
    store, diary = occ
    store.add_person("Ravi Kumar", e164="+10000000000")
    rel = diary.append_entry("ravi-kumar", "coffee — moving to Berlin", when="2026-06-18")
    assert rel == "12 - People/Ravi Kumar.md"

    import src.orchestrator.config as cfg

    md = (cfg.VAULT_PATH / rel).read_text()
    assert "## Timeline" in md
    assert "2026-06-18 — coffee — moving to Berlin" in md
    assert store.people_by_id()["ravi-kumar"].last_interaction == "2026-06-18"


def test_append_is_newest_first(occ):
    store, diary = occ
    store.add_person("Ravi Kumar")
    diary.append_entry("ravi-kumar", "older", when="2026-06-01")
    diary.append_entry("ravi-kumar", "newer", when="2026-06-18")
    import src.orchestrator.config as cfg

    md = (cfg.VAULT_PATH / "12 - People/Ravi Kumar.md").read_text()
    assert md.index("newer") < md.index("older")


def test_open_thread_section_does_not_bump_interaction(occ):
    store, diary = occ
    store.add_person("Ravi Kumar")
    diary.append_entry("ravi-kumar", "job interview next week", section="Open threads", when="2026-06-18")
    import src.orchestrator.config as cfg

    md = (cfg.VAULT_PATH / "12 - People/Ravi Kumar.md").read_text()
    assert "## Open threads" in md and "job interview next week" in md
    assert store.people_by_id()["ravi-kumar"].last_interaction is None


def test_unknown_person_raises(occ):
    _, diary = occ
    with pytest.raises(ValueError):
        diary.append_entry("nope", "x")


def test_open_thread_helpers_reject_dossier_traversal(occ):
    store, diary = occ
    from src.orchestrator import config

    person = store.add_person("Ravi Kumar")
    people = store.load_people()
    people.people[0].dossier = "../outside.md"
    store.save_people(people)
    outside = config.VAULT_PATH.parent / "outside.md"
    outside.write_text("## Open threads\n\n- must stay untouched\n")

    assert diary.list_open_threads() == []
    with pytest.raises(ValueError, match="escapes the vault"):
        diary.resolve_thread(person.id, "untouched")
    assert "must stay untouched" in outside.read_text()
