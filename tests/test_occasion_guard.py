from __future__ import annotations

from datetime import date

from src.brain.occasions import occasion_guard as og
from src.brain.occasions.models import (
    Festival,
    OccasionFlag,
    Person,
    PersonOccasions,
)


def test_yearless_rolls_to_next_year_when_passed():
    today = date(2026, 6, 18)
    assert og.next_occurrence("--03-15", today) == date(2027, 3, 15)
    assert og.next_occurrence("--12-25", today) == date(2026, 12, 25)


def test_leap_day_folds_in_non_leap_year():
    assert og.next_occurrence("--02-29", date(2027, 1, 1)) == date(2027, 2, 28)
    assert og.next_occurrence("--02-29", date(2028, 1, 1)) == date(2028, 2, 29)


def test_window_hits_birthday_and_festival():
    today = date(2026, 6, 18)
    people = [
        Person(
            id="ravi",
            name="Ravi",
            tier="inner",
            occasions=PersonOccasions(
                birthday=OccasionFlag(enabled=True, date="--06-20"),
                festivals=["diwali"],
            ),
        ),
        Person(
            id="sam",
            name="Sam",
            occasions=PersonOccasions(
                birthday=OccasionFlag(enabled=True, date="--12-01"),
            ),
        ),
        Person(
            id="out",
            name="Out",
            in_scope=False,
            occasions=PersonOccasions(
                birthday=OccasionFlag(enabled=True, date="--06-19"),
            ),
        ),
    ]
    festivals = {
        "diwali": Festival(label="Diwali", dates={"2026": "2026-06-25"}),
    }
    hits = og.window_hits(people, festivals, today, days=10)
    keys = [(h["person_id"], h["occasion"], h["days_out"]) for h in hits]
    assert ("ravi", "birthday", 2) in keys
    assert ("ravi", "diwali", 7) in keys
    assert all(k[0] != "sam" for k in keys)
    assert all(k[0] != "out" for k in keys)
    assert hits[0]["days_out"] <= hits[-1]["days_out"]
