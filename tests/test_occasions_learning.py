from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def occ(monkeypatch, tmp_path):
    monkeypatch.setenv("OCCASIONS_DIR", str(tmp_path / "occasions"))
    import src.orchestrator.config as cfg

    importlib.reload(cfg)
    import src.brain.occasions.store as store

    importlib.reload(store)
    import src.brain.occasions.learning as learning

    importlib.reload(learning)
    yield learning, store
    importlib.reload(cfg)


def test_identical_is_noop(occ):
    learning, store = occ
    assert learning.record_edit("Happy birthday!", "Happy birthday!") is False
    assert store.load_voice().learned_edits == []


def test_records_edit(occ):
    learning, store = occ
    assert learning.record_edit("draft text here friend", "final text here friend") is True
    v = store.load_voice()
    assert len(v.learned_edits) == 1
    assert v.learned_edits[0].before == "draft text here friend"


def test_promotes_repeatedly_cut_phrase(occ):
    learning, store = occ
    learning.record_edit(
        "Happy birthday! On this special day I wish you joy and health",
        "Happy birthday man, miss our treks!",
    )
    learning.record_edit(
        "Cheers! On this special day may all your dreams come true",
        "Cheers mate, proud of you!",
    )
    v = store.load_voice()
    assert len(v.learned_edits) == 2
    assert any("on this special day" in b for b in v.banned_cliches)


def test_one_off_edit_not_promoted(occ):
    learning, store = occ
    learning.record_edit("a unique phrasing never repeated again", "something else entirely")
    assert store.load_voice().banned_cliches == []
