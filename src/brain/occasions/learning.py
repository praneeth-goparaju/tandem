from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime

from src.brain.occasions import store
from src.brain.occasions.models import LearnedEdit, VoiceFile

_SHINGLE_N = 4


def _shingles(text: str, n: int = _SHINGLE_N) -> set[str]:
    words = re.findall(r"[a-z']+", (text or "").lower())
    if len(words) < n:
        return set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _promote_cliches(voice: VoiceFile) -> None:
    kept = " ".join(" ".join(re.findall(r"[a-z']+", e.after.lower())) for e in voice.learned_edits)
    cut_counts: Counter[str] = Counter()
    for e in voice.learned_edits:
        for sh in _shingles(e.before) - _shingles(e.after):
            cut_counts[sh] += 1
    existing = {b.lower() for b in voice.banned_cliches}
    for phrase, n in cut_counts.items():
        if n >= 2 and phrase not in kept and phrase not in existing:
            voice.banned_cliches.append(phrase)


def record_edit(draft_text: str, final_text: str, *, ts: str | None = None) -> bool:
    if (draft_text or "").strip() == (final_text or "").strip():
        return False
    voice = store.load_voice()
    voice.learned_edits.append(
        LearnedEdit(before=draft_text, after=final_text, ts=ts or datetime.now(UTC).isoformat())
    )
    _promote_cliches(voice)
    store.save_voice(voice)
    return True
