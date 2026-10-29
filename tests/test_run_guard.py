from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from src.orchestrator.runners.run_guard import (
    DIRECT_MUTATORS,
    HELPER_BUILTINS,
    HELPER_DENIED,
    HELPER_READ_TOOLS,
    HELPER_TYPE,
    OUTSIDE_CONTENT_TOOLS,
    READS_AFTER_WEB,
    VERIFY_BLOCKED_EFFECTS,
    HelperCap,
    OutsideContentState,
    WebRiskBarrier,
    bare_tool_name,
    confinement_denial,
    edit_denial,
    feature_denial,
    helper_tool_denial,
    scrubbed_env,
    vault_offlimits,
    verify_denial,
)


@pytest.mark.parametrize(("raw", "bare"), [
    ("Read", "read"),
    ("Glob", "find"),
    ("Grep", "grep"),
    ("mcp__brain-tools__mail_search", "mail_search"),
    ("Bash", "Bash"),
])
def test_bare_tool_name(raw: str, bare: str) -> None:
    assert bare_tool_name(raw) == bare


def _read(root: Path, path: str, roots: Sequence[Path] | None = None) -> str | None:
    return confinement_denial("Read", {"file_path": path}, roots or [root], cwd=root)


def test_allows_paths_within_the_vault(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "today.md").write_text("ok")
    assert _read(tmp_path, "notes/today.md") is None
    assert _read(tmp_path, "notes/new.md") is None
    assert _read(tmp_path, str(tmp_path / "notes" / "today.md")) is None


def test_rejects_traversal_absolute_paths_home_expansion_and_symlink_escapes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("no")
    os.symlink(outside, root / "escape")

    assert _read(root, "../outside/secret")
    assert _read(root, str(outside / "secret"))
    assert _read(root, "~/.env")
    assert _read(root, "escape/secret")


def test_rejects_hidden_files_and_directories_inside_the_vault(tmp_path: Path) -> None:
    assert _read(tmp_path, ".env")
    assert _read(tmp_path, ".obsidian/plugins/data.json")
    assert _read(tmp_path, "notes/.private/key")


@pytest.mark.parametrize("pattern", ["[.]obsidian/**", "?obsidian/**", "{.obsidian,notes}/**"])
def test_keeps_searches_out_of_hidden_vault_metadata(tmp_path: Path, pattern: str) -> None:
    def denial(tool: str, tool_input: dict) -> str | None:
        return confinement_denial(tool, tool_input, [tmp_path], cwd=tmp_path)

    assert denial("Glob", {"pattern": pattern})
    assert denial("Grep", {"pattern": "x", "glob": pattern})
    assert denial("Glob", {"pattern": "**/*.md"}) is None
    assert denial("Grep", {"pattern": "x", "glob": "*.{md,txt}"}) is None


def test_confinement_checks_each_tool_field(tmp_path: Path) -> None:
    def denial(tool: str, tool_input: dict) -> str | None:
        return confinement_denial(tool, tool_input, [tmp_path], cwd=tmp_path)

    inside = str(tmp_path / "notes" / "a.md")
    assert denial("Read", {"file_path": inside}) is None
    assert denial("Read", {"file_path": "/etc/passwd"})
    assert denial("Read", {})
    assert denial("Read", {"file_path": "  "})
    assert denial("Glob", {"pattern": "*.md"}) is None
    assert denial("Glob", {"pattern": "*.md", "path": "/tmp"})
    assert denial("Glob", {"pattern": "/etc/*"})
    assert denial("Glob", {"pattern": "../*"})
    assert denial("Glob", {"pattern": "notes/.hidden/*"})
    assert denial("Grep", {"pattern": "x"}) is None
    assert denial("Grep", {"pattern": "x", "path": "../"})
    assert denial("Grep", {"pattern": "x", "glob": "../*.md"})
    assert denial("mcp__brain-tools__mail_search", {"query": "/etc"}) is None


@pytest.fixture
def two_areas(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Family").mkdir(parents=True)
    (root / "05 - Health").mkdir()
    (root / "Family" / "notes.md").write_text("school run")
    (root / "05 - Health" / "secret.md").write_text("blood test")
    return root


SHARED_ONLY = "This chat may read only: Family/. Pass path (Glob, Grep) or file_path (Read) inside one of them."


@pytest.mark.parametrize(("tool", "tool_input"), [
    pytest.param("Glob", {"pattern": "**/*.md"}, id="glob-without-path"),
    pytest.param("Grep", {"pattern": "blood"}, id="grep-without-path"),
    pytest.param("Grep", {"pattern": "blood", "path": "05 - Health"}, id="grep-relative-private-folder"),
    pytest.param("Read", {"file_path": "05 - Health/secret.md"}, id="read-relative-private-file"),
    pytest.param("Glob", {"pattern": "*.md", "path": "."}, id="glob-in-the-vault-root"),
    pytest.param("Read", {"file_path": "~/x.md"}, id="read-in-home"),
    pytest.param("Read", {"file_path": "@Family/notes.md"}, id="read-with-an-at-sign"),
    pytest.param("Read", {"file_path": " ~/x.md"}, id="read-in-home-after-a-space"),
    pytest.param("Glob", {"pattern": "*.md", "path": "  "}, id="glob-with-a-blank-path"),
    pytest.param("Read", {"file_path": "Family/../05 - Health/secret.md"}, id="read-climbing-out"),
])
def test_a_limited_place_refuses_paths_outside_its_folders(
    two_areas: Path, tool: str, tool_input: dict,
) -> None:
    reason = confinement_denial(tool, tool_input, [two_areas / "Family"], cwd=two_areas)

    assert reason == SHARED_ONLY


def test_a_limited_place_trims_a_path_the_way_claude_code_does(two_areas: Path) -> None:
    # JavaScript's trim drops a leading byte-order mark, so Claude Code reads the path after it.
    secret = str(two_areas / "05 - Health" / "secret.md")

    assert _read(two_areas, chr(0xFEFF) + secret, [two_areas / "Family"]) == SHARED_ONLY


def test_a_limited_place_allows_paths_inside_its_folders(two_areas: Path) -> None:
    family = [two_areas / "Family"]

    assert _read(two_areas, "Family/notes.md", family) is None
    assert _read(two_areas, str(two_areas / "Family" / "notes.md"), family) is None
    assert _read(two_areas, "  Family/notes.md\n", family) is None
    assert confinement_denial(
        "Glob", {"pattern": "*.md", "path": "Family"}, family, cwd=two_areas,
    ) is None
    assert confinement_denial(
        "Grep", {"pattern": "school", "path": "Family/notes.md"}, family, cwd=two_areas,
    ) is None


def test_the_whole_vault_as_root_allows_a_missing_or_relative_path(two_areas: Path) -> None:
    vault = [two_areas]

    assert confinement_denial("Glob", {"pattern": "**/*.md"}, vault, cwd=two_areas) is None
    assert confinement_denial("Grep", {"pattern": "blood"}, vault, cwd=two_areas) is None
    assert _read(two_areas, "05 - Health/secret.md") is None
    reason = _read(two_areas, "../elsewhere.md")
    assert reason and "may read only" not in reason


def test_a_path_that_cannot_be_checked_is_refused_not_raised(two_areas: Path) -> None:
    too_long = "Family/" + "a" * 5000

    assert _read(two_areas, too_long, [two_areas / "Family"])
    assert _read(two_areas, too_long)


def test_the_refusal_names_files_and_folders_relative_to_the_vault(two_areas: Path) -> None:
    (two_areas / "Plans").mkdir()
    (two_areas / "Plans" / "Weekend.md").write_text("hike")
    roots = [two_areas / "Family", two_areas / "Plans" / "Weekend.md"]

    reason = _read(two_areas, "05 - Health/secret.md", roots)

    assert reason == (
        "This chat may read only: Family/, Plans/Weekend.md. "
        "Pass path (Glob, Grep) or file_path (Read) inside one of them."
    )


def _barrier(untrusted_input: bool = False) -> tuple[WebRiskBarrier, dict]:
    caller = {"untrusted_input": untrusted_input}
    return WebRiskBarrier(caller), caller


def test_after_web_access_reads_stay_but_mail_and_direct_effects_do_not() -> None:
    for name in ["mail_get_message", "contacts_get", "push_notify", "humans_log", "create_draft", "mail_archive"]:
        barrier, _ = _barrier()
        assert barrier.check("web_search") is None
        assert barrier.check(name), name
    barrier, _ = _barrier()
    assert barrier.check("web_search") is None
    for name in ["web_read", "reply_to_message", "store_finding", "propose_action", *sorted(READS_AFTER_WEB)]:
        assert barrier.check(name) is None, name


def test_forces_proposals_to_confirmation_and_blocks_repeated_searches() -> None:
    barrier, _ = _barrier()
    assert barrier.check("web_search") is None
    assert barrier.web_seen
    assert barrier.check("propose_action") is None
    assert barrier.check("web_search")


@pytest.mark.parametrize("areas", [None, ["family"]])
def test_a_run_makes_at_most_three_web_searches(areas) -> None:
    barrier = WebRiskBarrier({"untrusted_input": False, "areas": areas})
    for _ in range(3):
        assert barrier.check("web_search") is None
        assert barrier.check("web_read") is None
    assert "at most 3" in barrier.check("web_search")


def test_the_private_place_may_search_after_reading_but_not_after_reading_past_web() -> None:
    private = WebRiskBarrier({"untrusted_input": False, "areas": None})
    assert private.check("read") is None
    assert private.check("web_search") is None
    assert private.check("web_search") is None
    assert private.check("calendar_list_events") is None
    assert "No web search after reading" in private.check("web_search")


def test_blocks_search_after_any_private_or_local_tool_call() -> None:
    barrier, _ = _barrier()
    assert barrier.check("read") is None
    assert barrier.check("web_search")


def test_propagates_private_and_external_read_taint_into_later_python_tool_calls() -> None:
    barrier, caller = _barrier()
    assert barrier.check("mail_get_message") is None
    assert caller["untrusted_input"] is True
    assert barrier.check("propose_action") is None


@pytest.mark.parametrize("name", ["humans_read", "occasions_get_dossier"])
def test_taints_dossier_output(name: str) -> None:
    barrier, caller = _barrier()
    assert barrier.check(name) is None
    assert caller["untrusted_input"] is True
    assert barrier.check("humans_log")


def test_blocks_direct_mutation_after_initial_or_read_derived_taint() -> None:
    tainted, _ = _barrier(True)
    assert tainted.check("create_draft")
    barrier, _ = _barrier()
    assert barrier.check("mail_get_message") is None
    assert barrier.check("humans_log")


@pytest.mark.parametrize("name", sorted(DIRECT_MUTATORS))
def test_a_private_chat_keeps_direct_writers_after_reading_but_not_after_web(name: str) -> None:
    for first in ("mail_get_message", "read", "calendar_list_events", "intake_pending"):
        private = WebRiskBarrier({"untrusted_input": False, "areas": None, "is_reply": True})
        assert private.check(first) is None
        assert private.check(name) is None, (first, name)
    after_web = WebRiskBarrier({"untrusted_input": False, "areas": None, "is_reply": True})
    assert after_web.check("web_search") is None
    assert after_web.check(name)


@pytest.mark.parametrize("name", sorted(DIRECT_MUTATORS))
def test_a_message_that_starts_with_outside_content_keeps_the_block(name: str) -> None:
    quoted = WebRiskBarrier({"untrusted_input": True, "areas": None, "is_reply": True})
    assert quoted.check(name)


@pytest.mark.parametrize("name", sorted(DIRECT_MUTATORS))
def test_an_unattended_private_run_may_only_draft_after_reading_mail(name: str) -> None:
    scheduled = WebRiskBarrier({"untrusted_input": False, "areas": None, "is_reply": False})
    assert scheduled.check("mail_get_message") is None
    if name == "create_draft":
        assert scheduled.check(name) is None
    else:
        assert scheduled.check(name), name


@pytest.mark.parametrize("name", sorted(DIRECT_MUTATORS))
def test_the_shared_place_still_blocks_direct_writers_after_reading(name: str) -> None:
    shared = WebRiskBarrier({"untrusted_input": False, "areas": ["family"]})
    assert shared.check("read") is None
    assert shared.check(name)


def test_keeps_clean_diary_capture_functional_after_its_narrow_id_lookup() -> None:
    barrier, caller = _barrier()
    assert barrier.check("humans_find") is None
    assert caller["untrusted_input"] is True
    assert barrier.check("humans_log") is None


def test_does_not_let_a_people_lookup_unlock_unrelated_direct_writers() -> None:
    barrier, _ = _barrier()
    assert barrier.check("humans_find") is None
    for name in ["create_draft", "intake_dismiss", "watch_search"]:
        assert barrier.check(name), name


def test_barrier_marks_caller_untrusted() -> None:
    barrier, caller = _barrier()
    assert barrier.check("mail_search") is None
    assert caller["untrusted_input"] is True


def test_verify_denial_blocks_every_direct_mutator() -> None:
    for name in DIRECT_MUTATORS:
        assert verify_denial(name), name
    assert verify_denial("propose_action") is None
    assert verify_denial("mail_search") is None


@pytest.mark.parametrize("name", [
    "action_note_complete", "complete_reminder", "create_calendar_event",
    "create_contact", "create_reminder", "mail_archive",
    "tasks_create", "update_calendar_event", "update_contact",
])
def test_verify_denial_blocks_tools_with_real_effects(name: str) -> None:
    assert verify_denial(name)


@pytest.mark.parametrize("name", [
    "calendar_list_events", "contacts_search", "garmin_daily", "mail_get_message",
    "reminders_list", "store_finding", "push_notify", "reply_to_message",
    "send_bot_message", "web_search",
])
def test_verify_denial_allows_reads_and_slack_off_delivery(name: str) -> None:
    assert verify_denial(name) is None


def test_feature_denial_names_the_switch():
    assert feature_denial("draw_chart", {"BRAIN_MEDIA": "0"}) == "draw_chart is off (BRAIN_MEDIA=0)"
    assert feature_denial("draw_chart", {}) is None
    assert feature_denial("follow_ups_list", {"BRAIN_FOLLOWUPS": "off"}).startswith("follow_ups_list is off")
    assert feature_denial("store_finding", {"BRAIN_MEDIA": "0"}) is None


@pytest.mark.parametrize(("tool", "switch", "other"), [
    ("draw_chart", "BRAIN_MEDIA", "BRAIN_FOLLOWUPS"),
    ("draw_image", "BRAIN_MEDIA", "BRAIN_FOLLOWUPS"),
    ("follow_ups_digest", "BRAIN_FOLLOWUPS", "BRAIN_MEDIA"),
    ("follow_ups_list", "BRAIN_FOLLOWUPS", "BRAIN_MEDIA"),
    ("follow_ups_add", "BRAIN_FOLLOWUPS", "BRAIN_MEDIA"),
    ("follow_ups_update", "BRAIN_FOLLOWUPS", "BRAIN_MEDIA"),
])
def test_each_switch_gates_its_own_tools_only(tool: str, switch: str, other: str) -> None:
    assert feature_denial(tool, {switch: "0"}) == f"{tool} is off ({switch}=0)"
    assert feature_denial(tool, {switch: "1", other: "0"}) is None


def test_outside_content_tools_match_the_agreed_list() -> None:
    assert OUTSIDE_CONTENT_TOOLS == {
        "mail_search", "mail_get_message", "mail_list_attachments", "mail_get_attachment",
        "calendar_list_events", "calendar_get_event", "contacts_search", "contacts_get",
        "web_search", "web_read", "intake_pending", "search_houses", "process_watches",
        "public_discovery_candidates",
    }


def test_outside_content_state_marks_tools_and_results() -> None:
    caller: dict = {}
    state = OutsideContentState(caller, unattended=True)
    state.note_tool("read")
    state.note_result({"content": []})
    assert not state.seen
    assert "outside_content" not in caller
    assert not state.blocks_edits

    state.note_tool("mail_search")
    assert state.seen
    assert caller["outside_content"] is True
    assert state.blocks_edits

    flagged_caller: dict = {}
    flagged = OutsideContentState(flagged_caller, unattended=True)
    flagged.note_result({"outside_content": True})
    assert flagged.seen
    assert flagged_caller["outside_content"] is True

    chat_caller: dict = {}
    chat = OutsideContentState(chat_caller, unattended=False)
    chat.note_result({"outside_content": True})
    assert chat_caller["outside_content"] is True
    assert not chat.seen
    chat.note_tool("web_read")
    assert chat.seen
    assert not chat.blocks_edits

    assert OutsideContentState({}, unattended=True, seen=True).blocks_edits


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    health = root / "05 - Health"
    health.mkdir(parents=True)
    (root / "05 - Health-old").mkdir()
    (root / "01 - Daily Notes" / "2026" / "10").mkdir(parents=True)
    (root / "Brain Agents" / "Lessons").mkdir(parents=True)
    (health / "tracker.md").write_text("0123456789" * 20)
    (root / "05 - Health-old" / "x.md").write_text("old")
    (root / "01 - Daily Notes" / "2026" / "10" / "2026-10-15.md").write_text("today")
    (root / "Brain Agents" / "Lessons" / "health.md").write_text("lesson")
    (root / "CLAUDE.md").write_text("rules")
    (tmp_path / "outside.md").write_text("outside")
    os.symlink(tmp_path / "outside.md", health / "link.md")
    return root


HEALTH = ("05 - Health/",)
E_ACUTE = chr(0xE9)


def _denial(
    vault: Path, tool: str, tool_input: dict, edit_paths: Sequence[str] = HEALTH,
    state: OutsideContentState | None = None,
) -> str | None:
    return edit_denial(
        tool, tool_input, vault_root=vault, edit_paths=edit_paths,
        offlimits=vault_offlimits({}, "01 - Daily Notes"),
        state=state or OutsideContentState({}, unattended=False),
    )


def _edit(path: str | Path, old: str = "0123", new: str = "3210", **extra: object) -> dict:
    return {"file_path": str(path), "old_string": old, "new_string": new, **extra}


def _write(path: str | Path, content: str = "x") -> dict:
    return {"file_path": str(path), "content": content}


def test_edit_relative_path_resolves_against_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert _denial(vault, "Edit", _edit("05 - Health/tracker.md")) is None
    assert _denial(vault, "Write", _write("05 - Health/new.md")) is None
    reason = _denial(vault, "Edit", _edit("05 - Health-old/x.md"))
    assert reason and "not inside" in reason
    assert _denial(vault, "Edit", _edit("CLAUDE.md"))


def test_edit_refused_outside_edit_paths_and_for_prefix_lookalike(vault: Path) -> None:
    reason = _denial(vault, "Edit", _edit(vault / "05 - Health-old" / "x.md"))
    assert reason and "not inside" in reason
    assert _denial(vault, "Edit", _edit(vault / "05 - Health-old" / "x.md"), []) is not None

    only_tracker = ["05 - Health/tracker.md"]
    assert _denial(vault, "Edit", _edit("05 - Health/tracker.md"), only_tracker) is None
    reason = _denial(vault, "Write", _write("05 - Health/tracker.md.bak"), only_tracker)
    assert reason and "not inside" in reason


@pytest.mark.parametrize(("target", "edit_paths", "word"), [
    ("05 - Health/link.md", ["05 - Health/"], "symlink"),
    (".obsidian/app.json", [".obsidian/"], "hidden"),
    ("01 - Daily Notes/2026/10/2026-10-15.md", ["01 - Daily Notes/"], "off limits"),
    ("Brain Agents/Lessons/health.md", ["Brain Agents/"], "off limits"),
    ("CLAUDE.md", ["CLAUDE.md"], "off limits"),
])
def test_edit_refused_for_symlink_hidden_daily_note_and_prompt_loaded(
    vault: Path, target: str, edit_paths: list[str], word: str,
) -> None:
    for path in (target, str(vault / target)):
        for tool, tool_input in (("Edit", _edit(path)), ("Write", _write(path))):
            reason = _denial(vault, tool, tool_input, edit_paths)
            assert reason and word in reason, (tool, path)


@pytest.mark.parametrize("target", ["../outside.md", "/etc/hosts"])
@pytest.mark.parametrize("edit_paths", [["05 - Health/"], ["../"], ["/etc/"], [""], []])
def test_edit_refused_for_traversal_and_paths_outside_the_vault(
    vault: Path, target: str, edit_paths: list[str],
) -> None:
    for tool, tool_input in (("Edit", _edit(target)), ("Write", _write(target))):
        reason = _denial(vault, tool, tool_input, edit_paths)
        assert reason and "not inside" in reason, (tool, target)


@pytest.mark.parametrize("target", [
    "~/.env", "~", "@05 - Health/tracker.md", " ~/notes.md", "~other/05 - Health/tracker.md",
])
def test_edit_refuses_a_path_claude_code_and_the_rule_could_read_differently(
    vault: Path, target: str,
) -> None:
    for tool, tool_input in (("Edit", _edit(target)), ("Write", _write(target))):
        reason = _denial(vault, tool, tool_input)
        assert reason == (
            "write the path as Claude Code sees it, relative to the vault or absolute"
        ), (tool, target)


def test_edit_applies_the_rules_to_the_path_after_dot_dot_is_resolved(vault: Path) -> None:
    allowed = ["05 - Health/", "CLAUDE.md"]
    assert _denial(vault, "Edit", _edit("05 - Health/../05 - Health/tracker.md"), allowed) is None
    reason = _denial(vault, "Edit", _edit("05 - Health/../CLAUDE.md"), allowed)
    assert reason and "off limits" in reason


def test_edit_accepts_both_spellings_of_a_vault_reached_through_a_symlink(
    vault: Path, tmp_path: Path,
) -> None:
    (tmp_path / "links").mkdir()
    alias = tmp_path / "links" / "alias"
    os.symlink(vault, alias)

    def denial(path: str | Path, edit_paths: Sequence[str] = HEALTH) -> str | None:
        return edit_denial(
            "Edit", _edit(path), vault_root=alias, edit_paths=edit_paths,
            offlimits=vault_offlimits({}, "01 - Daily Notes"),
            state=OutsideContentState({}, unattended=False),
        )

    for path in ("05 - Health/tracker.md", alias / "05 - Health" / "tracker.md",
                 vault / "05 - Health" / "tracker.md"):
        assert denial(path) is None, path
    assert "off limits" in (denial(alias / "CLAUDE.md", ["CLAUDE.md"]) or "")
    assert "off limits" in (denial(alias / "05 - Health" / ".." / "CLAUDE.md", ["CLAUDE.md"]) or "")
    assert "symlink" in (denial(alias / "05 - Health" / "link.md") or "")
    assert "not inside" in (denial(alias / ".." / "outside.md") or "")
    assert "not inside" in (denial("../outside.md") or "")
    assert "not inside" in (denial("../alias/05 - Health/tracker.md") or "")
    assert denial("../vault/05 - Health/tracker.md") is None


def test_write_only_creates_files(vault: Path) -> None:
    from src.orchestrator.runners.run_guard import _WRITE_LIMIT

    health = vault / "05 - Health"

    reason = _denial(vault, "Write", _write(health / "tracker.md"))
    assert reason and "exists" in reason
    reason = _denial(vault, "Write", _write(health))
    assert reason and "exists" in reason
    assert _denial(vault, "Write", _write(health / "new.md")) is None
    reason = _denial(vault, "Write", _write(health / "missing" / "new.md"))
    assert reason and "folder" in reason

    reason = _denial(vault, "Write", _write(health / "big.md", "x" * 262145))
    assert reason and "256 KB" in reason
    assert _denial(vault, "Write", _write(health / "big.md", "x" * 262144)) is None
    two_byte_text = E_ACUTE * (_WRITE_LIMIT // len(E_ACUTE.encode()) + 1)
    assert len(two_byte_text) < _WRITE_LIMIT < len(two_byte_text.encode())
    assert _denial(vault, "Write", _write(health / "big.md", two_byte_text))


def test_edit_refused_when_it_removes_more_than_a_quarter(vault: Path) -> None:
    tracker = vault / "05 - Health" / "tracker.md"
    text = tracker.read_text()
    assert len(text.encode()) == 200

    reason = _denial(vault, "Edit", _edit(tracker, text[:60], ""))
    assert reason and "quarter" in reason
    assert _denial(vault, "Edit", _edit(tracker, text[:50], "")) is None
    assert _denial(vault, "Edit", _edit(tracker, text[:51], ""))
    assert _denial(vault, "Edit", _edit(tracker, text[:60], "x" * 60)) is None

    assert _denial(vault, "Edit", _edit(tracker, text[:10], "")) is None
    reason = _denial(vault, "Edit", _edit(tracker, text[:10], "", replace_all=True))
    assert reason and "quarter" in reason


def test_edit_shrink_rule_measures_bytes_not_characters(vault: Path) -> None:
    from src.orchestrator.runners.run_guard import _SHRINK_FLOOR

    text = E_ACUTE * 50 + "a" * 100
    note = vault / "05 - Health" / "umlauts.md"
    note.write_text(text)
    removable_share = 1 - _SHRINK_FLOOR
    removed = E_ACUTE * (int(removable_share * len(text.encode())) // len(E_ACUTE.encode()) + 1)
    assert len(removed) <= removable_share * len(text)
    assert len(removed.encode()) > removable_share * len(text.encode())

    reason = _denial(vault, "Edit", _edit(note, removed, ""))

    assert reason and "quarter" in reason


def test_edit_changes_existing_files_only(vault: Path) -> None:
    # Claude Code's Edit creates a missing file when old_string is empty.
    for old in ("a", ""):
        reason = _denial(vault, "Edit", _edit("05 - Health/missing.md", old, "text"))
        assert reason and "existing" in reason, old
    reason = _denial(vault, "Edit", _edit("05 - Health/missing/new.md", "", "text"))
    assert reason and "existing" in reason
    reason = _denial(vault, "Edit", _edit("05 - Health"))
    assert reason and "existing" in reason
    assert _denial(vault, "Write", _write("05 - Health/missing.md")) is None


QUARTER = "more than a quarter"
NOT_FOUND = "old_string was not found in the file; re-read it and try again"
CURLY = chr(0x2019)


def test_a_crlf_file_is_measured_as_the_lf_text_claude_code_edits(vault: Path) -> None:
    note = vault / "05 - Health" / "windows.md"
    lines = [f"line {number} of the training log" for number in range(10)]
    note.write_bytes("\r\n".join(lines).encode())

    reason = _denial(vault, "Edit", _edit(note, "\n".join(lines), ""))

    assert reason and QUARTER in reason
    assert _denial(vault, "Edit", _edit(note, "line 3 of", "line three of")) is None


def test_curly_apostrophes_are_matched_as_claude_code_matches_them(vault: Path) -> None:
    note = vault / "05 - Health" / "phone.md"
    text = f"Don{CURLY}t skip the warm-up. It{CURLY}s the knee. " * 4
    note.write_text(text)
    straight = text.replace(CURLY, "'")

    reason = _denial(vault, "Edit", _edit(note, straight[:150], ""))

    assert reason and QUARTER in reason
    assert _denial(vault, "Edit", _edit(note, "Don't skip", "Never skip")) is None
    reason = _denial(vault, "Edit", _edit(note, "It's the knee. ", "", replace_all=True))
    assert reason and QUARTER in reason


def test_edit_with_text_missing_from_the_file_is_refused(vault: Path) -> None:
    reason = _denial(vault, "Edit", _edit("05 - Health/tracker.md", "not in the file", ""))

    assert reason == NOT_FOUND


def test_a_file_that_is_not_text_is_refused(vault: Path) -> None:
    note = vault / "05 - Health" / "scan.md"
    note.write_bytes(b"\xff\x00\xfe binary \x80\x81")

    reason = _denial(vault, "Edit", _edit(note, "binary", ""))

    assert reason == "the file is not text this rule can measure"


def test_a_utf16_file_is_decoded_before_it_is_measured(vault: Path) -> None:
    note = vault / "05 - Health" / "export.md"
    text = "weight: 70 kg\n" * 10
    note.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))

    reason = _denial(vault, "Edit", _edit(note, text[:100], ""))

    assert reason and QUARTER in reason
    assert _denial(vault, "Edit", _edit(note, "70 kg", "71 kg")) is None


SPACES = "".join(char for char in map(chr, range(0x3001)) if char.isspace())


@pytest.mark.parametrize("name", ["log.txt", "notes.md", "notes.MDX"])
def test_the_text_assumed_written_is_never_longer_than_new_string(name: str) -> None:
    from src.orchestrator.runners.run_guard import _written_new

    samples = ("", "x", "x \n", "x" + SPACES, SPACES + "x" + SPACES + "\r\ny" + SPACES, "a\rb\r\nc\n")
    for new in samples:
        assert len(_written_new(new, Path(name))) <= len(new), repr(new)


def test_trailing_space_claude_code_strips_outside_markdown_counts_as_removed(vault: Path) -> None:
    note = vault / "05 - Health" / "log.txt"
    note.write_text("0123456789" * 20)
    padded = "x" + " " * 120

    reason = _denial(vault, "Edit", _edit(note, "0123456789" * 12, padded))
    assert reason and QUARTER in reason

    markdown = vault / "05 - Health" / "kept.md"
    markdown.write_text("0123456789" * 20)
    assert _denial(vault, "Edit", _edit(markdown, "0123456789" * 12, padded)) is None


def test_edit_refused_after_outside_content_in_unattended_run(vault: Path) -> None:
    tracker = vault / "05 - Health" / "tracker.md"
    seen = OutsideContentState({}, unattended=True, seen=True)
    for tool, tool_input in (
        ("Edit", _edit(tracker)),
        ("Write", _write(vault / "05 - Health" / "new.md")),
    ):
        reason = _denial(vault, tool, tool_input, HEALTH, seen)
        assert reason and "propose_action" in reason, tool

    chat = OutsideContentState({}, unattended=False, seen=True)
    assert _denial(vault, "Edit", _edit(tracker), HEALTH, chat) is None
    assert _denial(vault, "Edit", _edit(tracker), HEALTH, OutsideContentState({}, unattended=True)) is None


def test_edit_refused_for_symlinked_folders_and_dangling_links(vault: Path, tmp_path: Path) -> None:
    health = vault / "05 - Health"
    os.symlink(vault / "05 - Health-old", health / "shared")
    os.symlink(tmp_path / "not-created-yet.md", health / "dangling.md")

    for path in ("05 - Health/shared/x.md", "05 - Health/dangling.md"):
        for tool, tool_input in (("Edit", _edit(path)), ("Write", _write(path))):
            reason = _denial(vault, tool, tool_input)
            assert reason and "symlink" in reason, (tool, path)


def test_edit_offlimits_match_ignores_case_and_unicode_form(vault: Path) -> None:
    # macOS treats these names as the same file, so the rule must too.
    composed = "Caf" + chr(0xE9) + " Rules.md"
    decomposed = "Cafe" + chr(0x301) + " Rules.md"
    assert composed != decomposed
    protected = [f"05 - Health/{composed}"]
    for name in (composed, composed.lower(), decomposed, decomposed.lower()):
        reason = edit_denial(
            "Edit", _edit(f"05 - Health/{name}"), vault_root=vault, edit_paths=HEALTH,
            offlimits=protected, state=OutsideContentState({}, unattended=False),
        )
        assert reason and "off limits" in reason, name


def test_edit_denial_leaves_other_tools_alone(vault: Path) -> None:
    assert _denial(vault, "Read", {"file_path": "/etc/hosts"}) is None
    assert _denial(vault, "mcp__brain-tools__propose_action", {"file_path": "/etc/hosts"}) is None


def test_edit_refused_not_raised_when_the_target_cannot_be_checked(vault: Path) -> None:
    too_long = "05 - Health/" + "a" * 5000
    lone_surrogate = chr(0xD800)
    for tool, tool_input in (
        ("Edit", _edit(too_long)),
        ("Write", _write(too_long)),
        ("Write", _write("05 - Health/new.md", lone_surrogate)),
        ("Edit", _edit("05 - Health/tracker.md", lone_surrogate, "")),
    ):
        reason = _denial(vault, tool, tool_input)
        assert reason and "could not be checked" in reason, (tool, tool_input["file_path"][-20:])


def test_helper_tool_sets_hold_no_effect_tools() -> None:
    assert set(HELPER_BUILTINS) == {"Read", "Glob", "Grep"}
    assert not set(HELPER_BUILTINS) & set(HELPER_DENIED)
    assert {"Agent", "Edit", "Write", "Bash"} <= set(HELPER_DENIED)
    assert OUTSIDE_CONTENT_TOOLS - {"web_search", "web_read"} <= HELPER_READ_TOOLS
    assert not {"web_search", "web_read"} & HELPER_READ_TOOLS
    effects = DIRECT_MUTATORS | VERIFY_BLOCKED_EFFECTS | {
        "propose_action", "push_notify", "reply_to_message", "send_bot_message", "store_finding",
    }
    assert not HELPER_READ_TOOLS & effects


def test_helper_cap_counts_starts_and_checks_type() -> None:
    cap = HelperCap(2)
    helper = {"subagent_type": HELPER_TYPE}
    assert cap.check("Agent", helper) is None
    assert cap.check("Agent", helper) is None
    reason = cap.check("Agent", helper)
    assert reason and "2" in reason

    other = HelperCap(2)
    reason = other.check("Agent", {"subagent_type": "general-purpose"})
    assert reason and HELPER_TYPE in reason
    assert other.check("Agent", {}) is not None
    assert other.check("Read", {}) is None
    assert other.check("mcp__brain-tools__mail_search", {}) is None
    assert other.check("Task", helper) is None

    assert HelperCap(0).check("Agent", helper) is not None

    task = HelperCap(1)
    reason = task.check("Task", {"subagent_type": "general-purpose"})
    assert reason and HELPER_TYPE in reason
    assert task.check("Task", helper) is None
    assert task.check("Task", helper)


def test_helper_tool_denial_applies_only_inside_a_helper() -> None:
    propose = "mcp__brain-tools__propose_action"
    assert helper_tool_denial({"agent_id": "x", "tool_name": propose}, allowed={"Read"})
    assert helper_tool_denial({"tool_name": propose}, allowed={"Read"}) is None
    assert helper_tool_denial({"agent_id": "x", "tool_name": "Read"}, allowed={"Read"}) is None
    assert helper_tool_denial({"agent_id": "x"}, allowed={"Read"})


def test_scrubbed_env_blanks_secrets_only() -> None:
    environ = {
        "SLACK_BOT_TOKEN": "x", "GEMINI_API_KEY": "y", "ICLOUD_APP_PASSWORD": "z",
        "SERVICE_CREDENTIALS_PATH": "p", "CLAUDE_CODE_OAUTH_TOKEN": "keep", "VAULT_PATH": "/v",
        "WEBHOOK_SECRET": "s", "GOOGLE_APPLICATION_CREDENTIALS": "c",
    }
    assert scrubbed_env(environ) == {
        "SLACK_BOT_TOKEN": "", "GEMINI_API_KEY": "", "ICLOUD_APP_PASSWORD": "",
        "SERVICE_CREDENTIALS_PATH": "", "WEBHOOK_SECRET": "", "GOOGLE_APPLICATION_CREDENTIALS": "",
    }


@pytest.mark.parametrize("name", ["PATH", "HOME", "KEYBOARD", "TOKEN_COUNT", "CLAUDECODE"])
def test_scrubbed_env_leaves_other_names_alone(name: str) -> None:
    assert scrubbed_env({name: "v"}) == {}


def test_vault_offlimits_lists_prompt_loaded_paths() -> None:
    config = {
        "allowed_roots": ["Instructions/", "11 - Templates/"],
        "agents": {"career": ["CLAUDE.md", "Instructions/Rule Zero.md"]},
    }
    result = vault_offlimits(config, "01 - Daily Notes")
    assert result == [
        "01 - Daily Notes/", "CLAUDE.md", "VAULT_CONVENTIONS.md", "Instructions/",
        "Brain Agents/", "11 - Templates/", "Instructions/Rule Zero.md",
    ]
    assert len(result) == len(set(result))


def test_vault_offlimits_without_config_keeps_the_fixed_paths() -> None:
    assert vault_offlimits({}, "Daily/") == [
        "Daily/", "CLAUDE.md", "VAULT_CONVENTIONS.md", "Instructions/", "Brain Agents/",
    ]
    assert vault_offlimits({"allowed_roots": None, "agents": {"a": None}}, " Daily ") == [
        "Daily/", "CLAUDE.md", "VAULT_CONVENTIONS.md", "Instructions/", "Brain Agents/",
    ]
