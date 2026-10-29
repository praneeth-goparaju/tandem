import pytest
from pydantic import ValidationError

from src.brain.runtime.spec import AgentSpec, SlackDelivery


def test_spec_rejects_more_than_three_helpers_and_unknown_chat():
    assert AgentSpec(name="a", max_helpers=3).max_helpers == 3
    assert AgentSpec(name="a", chat="private").chat == "private"
    with pytest.raises(ValidationError):
        AgentSpec(name="a", max_helpers=4)
    with pytest.raises(ValidationError):
        AgentSpec(name="a", chat="group")


def test_spec_still_rejects_bash():
    with pytest.raises(ValidationError, match="unknown builtins"):
        AgentSpec(name="a", builtins=["Bash"])


def test_slack_delivery_accepts_direct_message_ids():
    assert SlackDelivery(channel_id="D0123ABC").channel_id == "D0123ABC"
    with pytest.raises(ValidationError):
        SlackDelivery(channel_id="X0123ABC")


def test_slack_delivery_still_accepts_channel_and_group_ids():
    for channel_id in ("C0123ABC", "G0123ABC"):
        assert SlackDelivery(channel_id=channel_id).channel_id == channel_id


def test_agents_without_the_new_fields_get_no_chat_paths_or_helpers(tmp_path):
    from src.brain.runtime.loader import LoadedConfig
    from src.orchestrator.registry import _build_config

    spec = AgentSpec(name="plain")
    assert (spec.chat, spec.vault_paths, spec.max_helpers) == (None, [], 0)
    agents, _, _ = _build_config(LoadedConfig(repo_root=tmp_path, agents={"plain": spec}))
    assert agents["plain"]["chat"] is None
    assert agents["plain"]["vault_paths"] == []
    assert agents["plain"]["max_helpers"] == 0


def test_registry_entry_carries_new_fields(tmp_path):
    from src.brain.runtime.loader import LoadedConfig
    from src.orchestrator.registry import _build_config

    spec = AgentSpec(name="alfred", chat="shared", vault_paths=["A/"], max_helpers=2)
    agents, _, _ = _build_config(LoadedConfig(repo_root=tmp_path, agents={"alfred": spec}))
    assert agents["alfred"]["chat"] == "shared"
    assert agents["alfred"]["vault_paths"] == ["A/"]
    assert agents["alfred"]["max_helpers"] == 2


def test_registry_entry_carries_before(tmp_path):
    from src.brain.runtime.loader import LoadedConfig
    from src.orchestrator.registry import _build_config

    specs = {
        "a": AgentSpec(name="a", before="src.brain.followups.collect"),
        "b": AgentSpec(name="b"),
    }
    agents, _, _ = _build_config(LoadedConfig(repo_root=tmp_path, agents=specs))
    assert agents["a"]["before"] == "src.brain.followups.collect"
    assert agents["b"]["before"] is None


@pytest.mark.parametrize("module", ["src.brain.followups.collect", "src.brain", "src.media_2.run"])
def test_spec_accepts_before_modules_under_src(module):
    assert AgentSpec(name="x", before=module).before == module


@pytest.mark.parametrize("module", [
    "os.system", "src..x", "src", "src.x.", "srcx.y", "src.Brain", "src/x", "src.x\n", "../evil",
])
def test_spec_rejects_before_modules_outside_src(module):
    with pytest.raises(ValidationError):
        AgentSpec(name="x", before=module)
