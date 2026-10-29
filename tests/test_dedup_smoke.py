from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brain.plugins.tasks._dedup import fuzzy_match, normalize_title, title_hash


def test_normalize_collapses_punctuation_and_stopwords() -> None:
    assert normalize_title("Apply to OpenAI!") == normalize_title("apply openai")
    assert normalize_title("Submit the OpenAI Codex application") == normalize_title(
        "submit OpenAI Codex application"
    )


def test_hash_identical_for_normalized_equivalents() -> None:
    assert title_hash("Apply to OpenAI!") == title_hash("apply openai")


def test_fuzzy_match_catches_rewordings() -> None:
    assert fuzzy_match("Apply to OpenAI Codex", "Submit OpenAI Codex application") is True


def test_fuzzy_match_rejects_unrelated() -> None:
    assert fuzzy_match("Apply to OpenAI", "Fix the bathroom sink") is False
