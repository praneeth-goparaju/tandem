from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def occ_store(monkeypatch, tmp_path):
    monkeypatch.setenv("OCCASIONS_DIR", str(tmp_path / "occasions"))
    import src.orchestrator.config as cfg

    importlib.reload(cfg)
    import src.brain.occasions.store as store

    importlib.reload(store)
    yield store
    importlib.reload(cfg)


def test_empty_when_missing(occ_store):
    assert occ_store.load_people().people == []
    assert occ_store.load_festivals().festivals == {}
    assert occ_store.load_voice().banned_cliches == []


def test_people_round_trip(occ_store):
    from src.brain.occasions.models import (
        OccasionFlag,
        PeopleFile,
        Person,
        PersonOccasions,
    )

    occ_store.save_people(
        PeopleFile(
            people=[
                Person(
                    id="ravi-kumar",
                    name="Ravi Kumar",
                    e164="+10000000000",
                    tier="inner",
                    occasions=PersonOccasions(
                        birthday=OccasionFlag(enabled=True, date="--03-15"),
                        festivals=["diwali"],
                    ),
                )
            ]
        )
    )
    loaded = occ_store.load_people()
    assert len(loaded.people) == 1
    p = loaded.people[0]
    assert p.id == "ravi-kumar"
    assert p.e164 == "+10000000000"
    assert p.tier == "inner"
    assert p.occasions.birthday.enabled is True
    assert p.occasions.birthday.date == "--03-15"
    assert p.occasions.festivals == ["diwali"]


def test_malformed_yaml_yields_empty(occ_store):
    import src.orchestrator.config as cfg

    cfg.OCCASIONS_PEOPLE.parent.mkdir(parents=True, exist_ok=True)
    cfg.OCCASIONS_PEOPLE.write_text("{ this: is: not: valid: yaml")
    assert occ_store.load_people().people == []


def test_reminder_stamp_deduplicates_and_rejects_missing_person(occ_store):
    from src.brain.occasions.models import PeopleFile, Person

    occ_store.save_people(PeopleFile(people=[Person(id="ravi", name="Ravi")]))
    assert not occ_store.already_reminded("ravi", "birthday", 2026)
    assert occ_store.stamp_reminded("ravi", "birthday", 2026, "2026-03-15T10:00:00Z")
    assert occ_store.already_reminded("ravi", "birthday", 2026)
    assert not occ_store.stamp_reminded("missing", "birthday", 2026)


def test_dossier_path_default_and_explicit(occ_store):
    from src.brain.occasions.models import Person

    assert occ_store.dossier_path(Person(id="ravi-kumar", name="Ravi Kumar")) == "12 - People/Ravi Kumar.md"
    assert (
        occ_store.dossier_path(Person(id="x", name="X", dossier="04 - Family/X.md"))
        == "04 - Family/X.md"
    )


def test_atomic_save_leaves_no_temp(occ_store):
    import src.orchestrator.config as cfg
    from src.brain.occasions.models import PeopleFile, Person

    occ_store.save_people(PeopleFile(people=[Person(id="x", name="X")]))
    assert list(cfg.OCCASIONS_DIR.glob("*.tmp")) == []
