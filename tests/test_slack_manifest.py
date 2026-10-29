from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "config" / "slack_app_manifest.yaml"


def test_manifest_has_the_home_tab_and_no_dm_scopes():
    manifest = yaml.safe_load(MANIFEST.read_text())
    app_home = manifest["features"]["app_home"]
    assert app_home["home_tab_enabled"] is True
    assert app_home["messages_tab_read_only_enabled"] is True
    events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "app_home_opened" in events and "message.im" not in events
    assert not [scope for scope in manifest["oauth_config"]["scopes"]["bot"] if scope.startswith("im:")]
    assert manifest["display_information"]["name"] == "Alfred"
    assert "reactions:write" in manifest["oauth_config"]["scopes"]["bot"]
    assert manifest["features"]["bot_user"]["display_name"] == "Alfred"


def test_slack_manifest_enables_approval_interactions_without_retired_scopes() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text())
    settings = manifest["settings"]
    scopes = set(manifest["oauth_config"]["scopes"]["bot"])

    assert settings["socket_mode_enabled"] is True
    assert settings["interactivity"]["is_enabled"] is True
    assert {"files:read", "files:write"} <= scopes
    assert "users:read" not in scopes
