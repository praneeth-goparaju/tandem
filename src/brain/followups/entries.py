from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    source: str
    ref: str
    who: str
    when: str
    text: str
    place: str
    project: str | None = None
    thread: str | None = None
