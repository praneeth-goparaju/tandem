from __future__ import annotations

from datetime import datetime

from src.brain.runtime.area_notes import area_notes_text
from src.brain.runtime.loader import LoadedConfig
from src.brain.runtime.spec import AgentSpec


def _cfg(tmp_path, body: str, **extra) -> LoadedConfig:
    return LoadedConfig(
        repo_root=tmp_path,
        agents={"family": AgentSpec(name="family", body=body)},
        profile={"user": {"name": "Sam"}},
        **extra,
    )


def test_block_between_the_markers_is_rendered_with_the_profile(tmp_path):
    body = (
        "Prompt before.\n"
        "{# area-notes #}\n"
        "Notes for {{ user.name }}\n"
        "{# /area-notes #}\n"
        "Prompt after.\n"
    )

    assert area_notes_text("family", _cfg(tmp_path, body)) == "Notes for Sam"


def test_body_without_both_markers_gives_an_empty_string(tmp_path):
    for body in (
        "A prompt for {{ user.name }} with no markers.",
        "{# area-notes #}\nOnly the opening marker.",
        "Only the closing marker.\n{# /area-notes #}",
        "{# /area-notes #}\nClosing before opening.\n{# area-notes #}",
    ):
        assert area_notes_text("family", _cfg(tmp_path, body)) == ""


def test_a_closing_marker_before_the_block_is_ignored(tmp_path):
    body = "{# /area-notes #}Stray.{# area-notes #}The notes.{# /area-notes #}"

    assert area_notes_text("family", _cfg(tmp_path, body)) == "The notes."


def test_unknown_agent_gives_an_empty_string(tmp_path):
    body = "{# area-notes #}Notes{# /area-notes #}"

    assert area_notes_text("nobody", _cfg(tmp_path, body)) == ""


def test_block_sees_the_same_context_as_the_prompt(tmp_path):
    body = (
        "{# area-notes #}"
        "On {{ weekday }} {{ today }}: see {{ source('plan') }}."
        "{# /area-notes #}"
    )
    cfg = _cfg(tmp_path, body, sources={"plan": "Plans/Family.md"})

    text = area_notes_text("family", cfg, at=datetime(2026, 10, 15, 9, 30))

    assert text == "On Thursday 2026-10-15: see Plans/Family.md."


def test_block_can_include_the_shared_fragments(tmp_path):
    fragments = tmp_path / "config" / "agents" / "_rules"
    fragments.mkdir(parents=True)
    (fragments / "shared.md").write_text("Shared rule for {{ user.name }}.\n", encoding="utf-8")
    body = '{# area-notes #}\n{% include "_rules/shared.md" %}\n{# /area-notes #}'

    assert area_notes_text("family", _cfg(tmp_path, body)) == "Shared rule for Sam."
