from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from src.brain.doctor import CHECKS, checks
from src.brain.runtime.area_notes import AREA_NOTES_CLOSE, AREA_NOTES_OPEN
from src.brain.runtime.loader import LoadedConfig
from src.brain.runtime.spec import AgentSpec, ModeSpec

REPO = Path(".")
MORNING = "mcp__brain-tools__send_morning_cards"
STORE = "mcp__brain-tools__store_finding"


def _spec(name, *, channel=None, **fields):
    delivery = {"slack": {"channel_id": channel}} if channel else None
    return AgentSpec(name=name, delivery=delivery, **fields)


def _cfg(*specs, integrations=None, profile=None, vault_instructions=None, modes=()):
    return LoadedConfig(
        repo_root=REPO,
        agents={spec.name: spec for spec in specs},
        modes={mode.name: mode for mode in modes},
        integrations=integrations or {},
        profile=profile or {},
        vault_instructions=vault_instructions or {},
    )


def _slack(**options):
    return {"notifier": {"type": "slack", "options": options}}


def test_the_new_checks_are_errors_and_the_judge_check_is_gone():
    severities = {check.name: check.severity for check in CHECKS}

    for name in (
        "check_chat_places", "check_vault_paths_offlimits",
        "check_area_notes_markers", "check_morning_tool_grant",
        "check_area_places_for_a_scheduled_morning",
    ):
        assert severities.get(name) == "error", name
    for name in (
        "check_area_places", "check_vault_paths_folder_slash", "check_followups_config",
        "check_follow_up_scheduled", "check_whisper_model", "check_media_dir_mode",
    ):
        assert severities.get(name) == "warn", name
    assert "check_judge_scope_render" not in severities


def _two_places():
    return (
        _spec("alfred", channel="C0EXAMPLE01", chat="shared"),
        _spec("family", channel="C0EXAMPLE01"),
        _spec("alfred_private", channel="D0EXAMPLE01", chat="private"),
        _spec("health", channel="D0EXAMPLE01"),
        _spec("inbox", channel="C0EXAMPLE07"),
        _spec("housekeeping", kind="system"),
    )


def test_two_places_with_one_chat_agent_each_pass():
    cfg = _cfg(*_two_places(), integrations=_slack())

    assert checks.check_chat_places(cfg, REPO) == []


def test_a_channel_with_two_areas_and_no_chat_agent_is_refused():
    cfg = _cfg(
        _spec("family", channel="C0SHARED"), _spec("career", channel="C0SHARED"),
        _spec("inbox", channel="C0ALONE"),
    )

    issues = checks.check_chat_places(cfg, REPO)

    assert len(issues) == 1
    message = issues[0].message
    assert "C0SHARED" in message and "family" in message and "career" in message
    assert "sets chat" in message
    assert issues[0].file == "agents/family.md"


def test_two_chat_agents_on_one_channel_are_refused():
    cfg = _cfg(
        _spec("alfred", channel="C0SHARED", chat="shared"),
        _spec("alfred_too", channel="C0SHARED", chat="private"),
    )

    issues = checks.check_chat_places(cfg, REPO)

    assert len(issues) == 1
    message = issues[0].message
    assert "C0SHARED" in message and "more than one chat agent" in message
    assert "alfred," in message and "alfred_too" in message


def test_one_place_is_held_by_one_agent_only():
    cfg = _cfg(
        _spec("alfred", channel="C0ONE", chat="shared"),
        _spec("alfred_b", channel="C0TWO", chat="shared"),
        _spec("alfred_private", channel="D0ONE", chat="private"),
    )

    issues = checks.check_chat_places(cfg, REPO)

    assert len(issues) == 1
    message = issues[0].message
    assert "chat: shared" in message and "alfred," in message and "alfred_b" in message


def test_channel_overrides_decide_which_agents_share_a_channel():
    same_placeholder = (
        _spec("family", channel="C0EXAMPLE04"), _spec("career", channel="C0EXAMPLE04"),
    )
    separated = _slack(channels={"family": {"channel_id": "C0F"}, "career": {"channel_id": "C0C"}})
    assert checks.check_chat_places(_cfg(*same_placeholder, integrations=separated), REPO) == []

    own_placeholders = (
        _spec("family", channel="C0EXAMPLE04"), _spec("career", channel="C0EXAMPLE05"),
    )
    joined = _slack(channels={"family": {"channel_id": "C0REAL"}, "career": {"channel_id": "C0REAL"}})
    issues = checks.check_chat_places(_cfg(*own_placeholders, integrations=joined), REPO)
    assert len(issues) == 1 and "C0REAL" in issues[0].message


def test_an_agent_without_slack_delivery_shares_no_channel():
    cfg = _cfg(_spec("housekeeping", kind="system"), _spec("discovery", kind="service"))

    assert checks.check_chat_places(cfg, REPO) == []


NO_PLACE = (
    "area inbox has no place: replies there are refused and it cannot appear in a "
    "morning message"
)
SCHEDULED_MORNING = ModeSpec(
    name="all_morning", schedule="0 7 * * *", pre=["housekeeping"], sequential=["alfred_private"],
)


def _place_issues(cfg):
    return (
        [issue.message for issue in checks.check_area_places(cfg, REPO)],
        [issue.message for issue in checks.check_area_places_for_a_scheduled_morning(cfg, REPO)],
    )


def test_every_area_in_a_place_and_the_private_place_set_up_raise_nothing():
    placed = [spec for spec in _two_places() if spec.name != "inbox"]

    assert _place_issues(_cfg(*placed)) == ([], [])
    assert _place_issues(_cfg(*placed, modes=[SCHEDULED_MORNING])) == ([], [])


def test_an_area_on_a_channel_without_a_chat_agent_has_no_place():
    assert _place_issues(_cfg(*_two_places())) == ([NO_PLACE], [])


@pytest.mark.parametrize(("mode", "raised"), [
    pytest.param(SCHEDULED_MORNING, True, id="scheduled-closing-run"),
    pytest.param(SCHEDULED_MORNING.model_copy(update={"schedule": None}), False, id="manual-closing-run"),
    pytest.param(
        SCHEDULED_MORNING.model_copy(update={"sequential": ["alfred"]}), False, id="scheduled-without-a-closing-run",
    ),
])
def test_a_scheduled_closing_run_makes_an_area_without_a_place_an_error(mode, raised):
    warnings, errors = _place_issues(_cfg(*_two_places(), modes=[mode]))

    assert (warnings, errors) == (([], [NO_PLACE]) if raised else ([NO_PLACE], []))


def test_the_overlay_channel_decides_whether_an_area_has_a_place():
    to_the_dm = _slack(channels={"inbox": {"channel_id": "D0EXAMPLE01"}})

    assert _place_issues(_cfg(*_two_places(), integrations=to_the_dm, modes=[SCHEDULED_MORNING])) == ([], [])


def test_the_private_place_needs_a_chat_agent_with_a_channel():
    without_private = [spec for spec in _two_places() if spec.name not in ("alfred_private", "health")]
    no_channel = [
        _spec("alfred_private", chat="private") if spec.name == "alfred_private" else spec
        for spec in _two_places() if spec.name not in ("health", "inbox")
    ]

    missing, _ = _place_issues(_cfg(*without_private))
    blank, _ = _place_issues(_cfg(*no_channel, modes=[SCHEDULED_MORNING]))

    assert missing == [NO_PLACE, "no agent has chat: private, so the owner's direct messages are no place"]
    assert blank == [
        "the private chat agent alfred_private has no Slack channel id, "
        "so the owner's direct messages are no place",
    ]


def test_vault_paths_inside_off_limits_folders_are_refused():
    cfg = _cfg(_spec(
        "family",
        vault_paths=["Brain Agents/Lessons/", "01 - Daily Notes/2026/", "03 - Family/"],
    ))

    issues = checks.check_vault_paths_offlimits(cfg, REPO)

    assert len(issues) == 2
    joined = "\n".join(issue.message for issue in issues)
    assert "'Brain Agents/Lessons/'" in joined and "'01 - Daily Notes/2026/'" in joined
    assert "03 - Family" not in joined
    assert {issue.file for issue in issues} == {"agents/family.md"}


def test_the_off_limits_folders_and_files_themselves_are_refused():
    entries = [
        "Brain Agents/", "01 - Daily Notes/", "Instructions/", "CLAUDE.md", "VAULT_CONVENTIONS.md",
    ]
    cfg = _cfg(_spec("family", vault_paths=entries))

    assert len(checks.check_vault_paths_offlimits(cfg, REPO)) == len(entries)


def test_vault_paths_are_compared_by_whole_path_segments():
    cfg = _cfg(_spec("family", vault_paths=[
        "01 - Daily Notes-old/", "Brain Agents Archive/", "Instructions.md", "CLAUDE.md.bak",
        "05 - Family/Brain Agents/",
    ]))

    assert checks.check_vault_paths_offlimits(cfg, REPO) == []


def test_vault_paths_are_compared_ignoring_case_like_the_edit_rule():
    cfg = _cfg(_spec("family", vault_paths=["brain agents/lessons/", "claude.md"]))

    assert len(checks.check_vault_paths_offlimits(cfg, REPO)) == 2


def test_the_daily_notes_folder_comes_from_the_profile():
    spec = _spec("family", vault_paths=["Journal/2026/", "01 - Daily Notes/2026/"])

    custom = _cfg(spec, profile={"notes": {"daily_notes_dir": "Journal"}})
    issues = checks.check_vault_paths_offlimits(custom, REPO)
    assert len(issues) == 1 and "'Journal/2026/'" in issues[0].message

    default = _cfg(spec, profile={"notes": {}})
    issues = checks.check_vault_paths_offlimits(default, REPO)
    assert len(issues) == 1 and "'01 - Daily Notes/2026/'" in issues[0].message


def test_prompt_loaded_files_are_off_limits_to_every_agent():
    vault_instructions = {
        "allowed_roots": ["Playbooks/"],
        "agents": {"career": ["03 - Career/Playbook.md"]},
    }
    cfg = _cfg(
        _spec("career", vault_paths=[
            "03 - Career/Playbook.md", "03 - Career/Notes.md", "Playbooks/Interviews.md",
        ]),
        _spec("health", vault_paths=["03 - Career/Playbook.md"]),
        vault_instructions=vault_instructions,
    )

    issues = checks.check_vault_paths_offlimits(cfg, REPO)

    by_file: dict[str, str] = {}
    for issue in issues:
        by_file[issue.file] = by_file.get(issue.file, "") + issue.message + "\n"
    assert set(by_file) == {"agents/career.md", "agents/health.md"}
    assert "'03 - Career/Playbook.md'" in by_file["agents/career.md"]
    assert "'Playbooks/Interviews.md'" in by_file["agents/career.md"]
    assert "Notes.md" not in by_file["agents/career.md"]
    assert "'03 - Career/Playbook.md'" in by_file["agents/health.md"]


@pytest.mark.parametrize("entry", ["", ".", "/", "./"])
def test_an_entry_naming_the_vault_root_fails_on_its_own(entry):
    vault_instructions = {"agents": {"career": ["03 - Career/Playbook.md", "03 - Career/Voice.md"]}}
    cfg = _cfg(_spec("health", vault_paths=[entry]), vault_instructions=vault_instructions)

    issues = checks.check_vault_paths_offlimits(cfg, REPO)

    assert len(issues) == 1
    assert issues[0].file == "agents/health.md"
    assert f"'{entry}'" in issues[0].message and "whole vault" in issues[0].message


def test_a_folder_entry_that_contains_the_daily_notes_folder_is_refused():
    spec = _spec("health", vault_paths=["Notes/"])

    custom = _cfg(spec, profile={"notes": {"daily_notes_dir": "Notes/Daily"}})
    issues = checks.check_vault_paths_offlimits(custom, REPO)
    assert len(issues) == 1
    assert "'Notes/'" in issues[0].message and "'Notes/Daily/'" in issues[0].message

    assert checks.check_vault_paths_offlimits(_cfg(spec), REPO) == []


def test_one_message_names_every_off_limits_path_a_folder_entry_contains():
    cfg = _cfg(
        _spec("career", vault_paths=["03 - Career/"]),
        vault_instructions={"agents": {"career": ["03 - Career/Playbook.md", "03 - Career/Voice.md"]}},
    )

    issues = checks.check_vault_paths_offlimits(cfg, REPO)

    assert len(issues) == 1
    assert "'03 - Career/Playbook.md'" in issues[0].message
    assert "'03 - Career/Voice.md'" in issues[0].message


def test_containment_is_by_whole_segments_ignoring_case_and_only_for_folder_entries():
    vault_instructions = {"agents": {"career": ["03 - Career/Playbook.md"]}}

    def issues_for(*entries):
        cfg = _cfg(_spec("career", vault_paths=list(entries)), vault_instructions=vault_instructions)
        return checks.check_vault_paths_offlimits(cfg, REPO)

    assert len(issues_for("03 - career/")) == 1
    assert issues_for("03 - Car/", "03 - Career-old/", "03 - Career") == []


@pytest.mark.parametrize(("entry", "vault_instructions"), [
    pytest.param("./03 - Career/", {"agents": {"c": ["03 - Career/Playbook.md"]}}, id="contains-dot-prefix"),
    pytest.param("03 - Career//", {"agents": {"c": ["03 - Career/Playbook.md"]}}, id="contains-double-slash"),
    pytest.param("03 - CAREER/", {"agents": {"c": ["03 - Career/Playbook.md"]}}, id="contains-case"),
    pytest.param(
        "03 - Carrie\u0300re/", {"agents": {"c": ["03 - Carri\u00e8re/Playbook.md"]}},
        id="contains-unicode-form",
    ),
    pytest.param("./Brain Agents/Lessons/", {}, id="inside-dot-prefix"),
    pytest.param("Brain Agents//Lessons/", {}, id="inside-double-slash"),
    pytest.param(
        "Carrie\u0300re/Notes/", {"allowed_roots": ["Carri\u00e8re/"]}, id="inside-unicode-form",
    ),
])
def test_a_spelling_that_differs_from_the_configuration_still_meets_it(entry, vault_instructions):
    cfg = _cfg(_spec("career", vault_paths=[entry]), vault_instructions=vault_instructions)

    assert len(checks.check_vault_paths_offlimits(cfg, REPO)) == 1


def test_an_entry_inside_an_off_limits_folder_is_reported_once_even_if_it_contains_one_too():
    cfg = _cfg(
        _spec("career", vault_paths=["Brain Agents/Lessons/"]),
        vault_instructions={"agents": {"career": ["Brain Agents/Lessons/career.md"]}},
    )

    issues = checks.check_vault_paths_offlimits(cfg, REPO)

    assert len(issues) == 1
    assert "is, or is inside" in issues[0].message and "'Brain Agents/'" in issues[0].message


@pytest.mark.parametrize(("entry", "warned"), [
    pytest.param("05 - Health", True, id="folder-without-its-slash"),
    pytest.param("Family/School", True, id="nested-folder-without-its-slash"),
    pytest.param("05 - Health/", False, id="folder"),
    pytest.param("Plans/Weekend.md", False, id="file"),
    pytest.param(".", False, id="the-vault-root-is-the-other-checks-finding"),
])
def test_an_entry_without_a_slash_or_an_extension_is_probably_a_folder(entry, warned):
    issues = checks.check_vault_paths_folder_slash(_cfg(_spec("health", vault_paths=[entry])), REPO)

    assert bool(issues) is warned
    if warned:
        assert issues[0].file == "agents/health.md"
        assert f"'{entry}'" in issues[0].message and "/" in issues[0].message


NOTES = f"{AREA_NOTES_OPEN}\nWhat the area covers.\n{AREA_NOTES_CLOSE}\n"


@pytest.mark.parametrize(("body", "refused"), [
    pytest.param(f"Intro.\n{NOTES}Morning checklist.", False, id="both-in-order"),
    pytest.param("No markers at all.", True, id="none"),
    pytest.param(f"{AREA_NOTES_OPEN}\nnever closed", True, id="only-the-opening-one"),
    pytest.param(f"never opened\n{AREA_NOTES_CLOSE}", True, id="only-the-closing-one"),
    pytest.param(f"{AREA_NOTES_CLOSE}\nbackwards\n{AREA_NOTES_OPEN}", True, id="wrong-order"),
])
def test_area_owners_need_both_notes_markers_in_order(body, refused):
    cfg = _cfg(_spec("family", kind="area_owner", body=body))

    issues = checks.check_area_notes_markers(cfg, REPO)

    assert bool(issues) is refused
    if refused:
        assert [issue.file for issue in issues] == ["agents/family.md"]
        assert AREA_NOTES_OPEN in issues[0].message and AREA_NOTES_CLOSE in issues[0].message


def test_only_area_owners_need_notes_markers():
    cfg = _cfg(
        _spec("discovery", kind="service", body="No markers."),
        _spec("housekeeping", kind="system", body="No markers."),
        _spec("plain", body="No markers."),
        _spec("family", kind="area_owner", body=NOTES),
    )

    assert checks.check_area_notes_markers(cfg, REPO) == []


def test_the_morning_message_tool_is_granted_only_to_the_private_chat_agent():
    cfg = _cfg(
        _spec("alfred_private", chat="private", mcp_tools=[MORNING, STORE]),
        _spec("family", mcp_tools=[MORNING]),
        _spec("alfred", chat="shared", mcp_tools=[MORNING]),
        _spec("health", reply_mcp_tools=[MORNING]),
        _spec("career", mcp_tools=[STORE]),
    )

    issues = checks.check_morning_tool_grant(cfg, REPO)

    assert sorted(issue.file for issue in issues) == [
        "agents/alfred.md", "agents/family.md", "agents/health.md",
    ]
    assert all("chat: private" in issue.message for issue in issues)


def test_the_private_chat_agent_may_not_hold_it_as_a_reply_tool():
    cfg = _cfg(_spec("alfred_private", chat="private", reply_mcp_tools=[MORNING]))

    assert len(checks.check_morning_tool_grant(cfg, REPO)) == 1


@pytest.fixture
def followups_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    monkeypatch.delenv("BRAIN_FOLLOWUPS", raising=False)
    (tmp_path / "profile.yaml").write_text("user: {name: Test}\n")
    return tmp_path


@pytest.mark.parametrize("overlay", ["empty", "missing"])
def test_a_clone_without_an_overlay_gets_no_followups_warning(tmp_path, monkeypatch, overlay):
    user_dir = tmp_path / "user"
    if overlay == "empty":
        user_dir.mkdir()
    monkeypatch.setenv("USER_DIR", str(user_dir))
    monkeypatch.delenv("BRAIN_FOLLOWUPS", raising=False)

    assert checks.check_followups_config(_cfg(), REPO) == []


def test_follow_ups_on_without_an_overlay_file_warn_that_every_project_folder_is_read(followups_overlay):
    issues = checks.check_followups_config(_cfg(), REPO)

    assert len(issues) == 1
    assert issues[0].file.endswith("followups.yaml")
    assert "every project folder" in issues[0].message


@pytest.mark.parametrize(
    "overlay",
    ['exclude_projects: ["-Users-me-work-acme"]\n', 'exclude_paths: ["acme"]\n'],
)
def test_follow_ups_with_an_overlay_file_that_excludes_something_raise_nothing(
    followups_overlay, overlay,
):
    (followups_overlay / "followups.yaml").write_text(overlay)

    assert checks.check_followups_config(_cfg(), REPO) == []


def test_an_overlay_file_that_names_no_exclusion_list_warns(followups_overlay):
    (followups_overlay / "followups.yaml").write_text("old_channels: []\n")

    issues = checks.check_followups_config(_cfg(), REPO)

    assert len(issues) == 1
    assert "exclude_projects" in issues[0].message and "every project folder" in issues[0].message


def test_an_overlay_file_with_explicitly_empty_exclusions_is_a_decision(followups_overlay):
    (followups_overlay / "followups.yaml").write_text("exclude_projects: []\nexclude_paths: []\n")

    assert checks.check_followups_config(_cfg(), REPO) == []


def test_an_overlay_file_that_cannot_be_read_is_reported_not_raised(followups_overlay):
    (followups_overlay / "followups.yaml").write_text("exclude_projects: [unclosed\n")

    issues = checks.check_followups_config(_cfg(), REPO)

    assert len(issues) == 1 and "cannot be read" in issues[0].message


def _morning(*agents, schedule="0 7 * * *"):
    return ModeSpec(name="all_morning", schedule=schedule, sequential=list(agents))


@pytest.mark.parametrize(
    "modes",
    [(), (_morning("alfred_private"),), (_morning("follow_up", "alfred_private", schedule=None),)],
    ids=["no modes", "a morning without it", "only a manual mode"],
)
def test_follow_ups_on_with_no_scheduled_follow_up_run_warn(followups_overlay, modes):
    issues = checks.check_follow_up_scheduled(_cfg(modes=modes), REPO)

    assert len(issues) == 1 and "follow_up" in issues[0].message


def test_a_scheduled_follow_up_run_or_the_switch_off_raise_nothing(followups_overlay, monkeypatch):
    scheduled = _cfg(modes=(_morning("follow_up", "alfred_private"),))
    assert checks.check_follow_up_scheduled(scheduled, REPO) == []
    monkeypatch.setenv("BRAIN_FOLLOWUPS", "off")
    assert checks.check_follow_up_scheduled(_cfg(), REPO) == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_follow_ups_switched_off_raise_nothing_without_an_overlay_file(followups_overlay, monkeypatch, value):
    monkeypatch.setenv("BRAIN_FOLLOWUPS", value)

    assert checks.check_followups_config(_cfg(), REPO) == []


@pytest.mark.parametrize("value", ["1", "true", "on", ""])
def test_follow_ups_switched_on_warn_without_an_overlay_file(followups_overlay, monkeypatch, value):
    monkeypatch.setenv("BRAIN_FOLLOWUPS", value)

    assert len(checks.check_followups_config(_cfg(), REPO)) == 1
MODEL_BYTES = b"pretend model weights"
MODEL_HASH = hashlib.sha256(MODEL_BYTES).hexdigest()


def _media_repo(tmp_path, sha256=MODEL_HASH):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "media.yaml").write_text(
        f'whisper:\n  model: ggml-small.bin\n  sha256: "{sha256}"\n  language: auto\n'
    )
    return tmp_path


def _model(tmp_path, data=MODEL_BYTES):
    path = tmp_path / "models" / "ggml-small.bin"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def media_env(monkeypatch):
    monkeypatch.delenv("BRAIN_MEDIA", raising=False)
    monkeypatch.delenv("BRAIN_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    return monkeypatch


def test_a_model_that_matches_its_pinned_hash_passes(tmp_path, media_env):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(_model(tmp_path)))

    assert checks.check_whisper_model(_cfg(), _media_repo(tmp_path)) == []


def test_a_pinned_hash_in_capitals_still_matches(tmp_path, media_env):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(_model(tmp_path)))

    assert checks.check_whisper_model(_cfg(), _media_repo(tmp_path, MODEL_HASH.upper())) == []


@pytest.mark.parametrize("value", [None, "", "  "])
def test_with_media_on_and_no_model_the_doctor_warns(tmp_path, media_env, value):
    if value is not None:
        media_env.setenv("BRAIN_WHISPER_MODEL", value)

    [issue] = checks.check_whisper_model(_cfg(), _media_repo(tmp_path, "TO-BE-PINNED"))

    assert issue.file == ".env"
    assert "BRAIN_WHISPER_MODEL is not set" in issue.message
    assert "voice notes get the type-it reply until the model is configured" in issue.message


def test_in_ci_a_missing_model_is_not_reported(tmp_path, media_env):
    media_env.setenv("BRAIN_DOCTOR_SKIP_RUNTIME", "1")
    assert checks.check_whisper_model(_cfg(), _media_repo(tmp_path, "TO-BE-PINNED")) == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_with_the_media_switch_off_a_missing_model_is_not_reported(tmp_path, media_env, value):
    media_env.setenv("BRAIN_MEDIA", value)
    assert checks.check_whisper_model(_cfg(), _media_repo(tmp_path, "TO-BE-PINNED")) == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_with_the_media_switch_off_the_model_is_not_checked(tmp_path, media_env, value):
    media_env.setenv("BRAIN_MEDIA", value)
    media_env.setenv("BRAIN_WHISPER_MODEL", str(tmp_path / "nowhere.bin"))

    assert checks.check_whisper_model(_cfg(), _media_repo(tmp_path)) == []


def test_a_model_file_that_is_missing_is_reported(tmp_path, media_env):
    missing = tmp_path / "nowhere.bin"
    media_env.setenv("BRAIN_WHISPER_MODEL", str(missing))

    [issue] = checks.check_whisper_model(_cfg(), _media_repo(tmp_path))

    assert issue.file == ".env"
    assert str(missing) in issue.message and "not a file" in issue.message


def test_a_model_that_is_a_folder_is_reported(tmp_path, media_env):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(tmp_path))

    [issue] = checks.check_whisper_model(_cfg(), _media_repo(tmp_path))

    assert "not a file" in issue.message


def test_a_model_that_differs_from_its_pinned_hash_is_reported(tmp_path, media_env):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(_model(tmp_path, b"something else")))

    [issue] = checks.check_whisper_model(_cfg(), _media_repo(tmp_path))

    assert issue.file == "config/media.yaml"
    assert "does not match" in issue.message and MODEL_HASH[:12] in issue.message


def test_a_hash_that_is_not_pinned_yet_says_so(tmp_path, media_env):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(_model(tmp_path)))

    [issue] = checks.check_whisper_model(_cfg(), _media_repo(tmp_path, "TO-BE-PINNED"))

    assert issue.file == "config/media.yaml"
    assert "not pinned" in issue.message and "TO-BE-PINNED" in issue.message


@pytest.mark.parametrize("pin", ["", "abc", "g" * 64, "a" * 63])
def test_a_malformed_pin_is_reported(tmp_path, media_env, pin):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(_model(tmp_path)))

    [issue] = checks.check_whisper_model(_cfg(), _media_repo(tmp_path, pin))

    assert issue.file == "config/media.yaml"
    assert "64 hex digits" in issue.message


def test_an_unreadable_media_config_is_reported_not_raised(tmp_path, media_env):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(_model(tmp_path)))

    [issue] = checks.check_whisper_model(_cfg(), tmp_path)

    assert issue.file == "config/media.yaml"


def test_the_shipped_media_config_is_one_the_check_can_read(tmp_path, media_env):
    media_env.setenv("BRAIN_WHISPER_MODEL", str(_model(tmp_path)))
    shipped = Path(__file__).resolve().parent.parent

    [issue] = checks.check_whisper_model(_cfg(), shipped)

    assert issue.file == "config/media.yaml"
    assert "64 hex digits" not in issue.message and "cannot be read" not in issue.message


def test_a_private_media_folder_passes_and_a_missing_one_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    assert checks.check_media_dir_mode(_cfg(), tmp_path) == []

    folder = tmp_path / "media"
    folder.mkdir()
    os.chmod(folder, 0o700)

    assert checks.check_media_dir_mode(_cfg(), tmp_path) == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777, 0o600])
def test_a_media_folder_with_any_other_mode_is_reported(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("USER_DIR", str(tmp_path))
    folder = tmp_path / "media"
    folder.mkdir()
    os.chmod(folder, mode)

    [issue] = checks.check_media_dir_mode(_cfg(), tmp_path)

    assert issue.file == str(folder)
    assert "0700" in issue.message and f"{mode:04o}" in issue.message
    os.chmod(folder, 0o700)


def test_a_media_folder_that_is_a_symlink_or_a_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o700)
    (tmp_path / "user" / "media").symlink_to(elsewhere)

    [issue] = checks.check_media_dir_mode(_cfg(), tmp_path)
    assert "real directory" in issue.message

    (tmp_path / "user" / "media").unlink()
    (tmp_path / "user" / "media").write_text("not a folder")
    [issue] = checks.check_media_dir_mode(_cfg(), tmp_path)
    assert "real directory" in issue.message


