from __future__ import annotations

import pytest

from src.brain.runtime.vault_paths import path_in_vault_paths

FAMILY = ("Family/",)


def test_a_path_inside_a_folder_entry_is_covered() -> None:
    assert path_in_vault_paths("Family/x.md", FAMILY)
    assert path_in_vault_paths("Family/School/term.md", FAMILY)


@pytest.mark.parametrize("path", [
    pytest.param("Family/../05 - Health/x.md", id="climbs-out-after-the-folder"),
    pytest.param("/Family/x.md", id="absolute"),
    pytest.param("./Family/x.md", id="dot-segment"),
    pytest.param("Family/./x.md", id="dot-segment-inside"),
    pytest.param("Family//x.md", id="empty-segment"),
    pytest.param("", id="empty"),
    pytest.param(".", id="the-vault-root"),
])
def test_a_path_that_is_not_one_canonical_relative_spelling_is_not_covered(path: str) -> None:
    assert not path_in_vault_paths(path, FAMILY)


def test_folder_entries_match_whole_segments_and_file_entries_match_exactly() -> None:
    assert not path_in_vault_paths("Family-old/x.md", FAMILY)
    assert path_in_vault_paths("Plans/Weekend.md", ("Plans/Weekend.md",))
    assert not path_in_vault_paths("Plans/Weekend.md.bak", ("Plans/Weekend.md",))
    assert not path_in_vault_paths("Family/x.md", ())
