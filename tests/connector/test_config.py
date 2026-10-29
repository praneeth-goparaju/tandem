from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from src.brain.connector.config import (
    REPO_ROOT,
    AlertLists,
    ConnectorConfig,
    Integrations,
    load_config,
    system_timezone,
    user_dir,
)
from src.brain.connector.layout import VaultLayout


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_user_dir_is_not_the_repo_folder_in_connector_tests() -> None:
    assert user_dir() != REPO_ROOT / "user"


def test_load_config_reads_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    data_dir = tmp_path / "data"
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"""
vault_path: ~/Vault
data_dir: {data_dir}
public_url: https://vault.example.com
allowed_redirect_uris:
  - https://claude.ai/api/mcp/auth_callback
  - https://claude.com/api/mcp/auth_callback
alert_lists:
  household: List A
timezone: Europe/London
""",
    )

    cfg = load_config(cfg_file)

    assert isinstance(cfg, ConnectorConfig)
    assert cfg.vault_path == tmp_path / "Vault"
    assert cfg.data_dir == data_dir
    assert cfg.public_url == "https://vault.example.com"
    assert cfg.allowed_redirect_uris == (
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    )
    assert cfg.alert_lists.household == "List A"
    assert cfg.timezone == "Europe/London"


def test_missing_vault_path_raises(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"data_dir: {tmp_path / 'data'}\npublic_url: https://vault.example.com\n",
    )

    with pytest.raises(ValueError, match="vault_path"):
        load_config(cfg_file)


def test_default_redirect_uri(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://vault.example.com\n",
    )

    cfg = load_config(cfg_file)

    assert cfg.allowed_redirect_uris == (
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    )
    assert cfg.alert_lists == AlertLists(household="Family")
    assert cfg.timezone == system_timezone()
    assert cfg.timezone_source == "this Mac"


def test_data_dir_is_created_private(tmp_path: Path) -> None:
    data_dir = tmp_path / "nested" / "connector-data"
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {data_dir}\npublic_url: https://vault.example.com\n",
    )

    cfg = load_config(cfg_file)

    assert cfg.data_dir == data_dir
    assert data_dir.is_dir()
    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700


def test_a_relative_data_dir_is_resolved_against_the_config_folder(tmp_path: Path) -> None:
    folder = tmp_path / "settings"
    folder.mkdir()
    cfg_file = _write(
        folder / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: connector-data\npublic_url: https://vault.example.com\n",
    )

    cfg = load_config(cfg_file)

    assert cfg.data_dir == folder / "connector-data"
    assert cfg.data_dir.is_absolute()
    assert cfg.data_dir.is_dir()


def test_data_dir_defaults_to_home_connector_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\npublic_url: https://vault.example.com\n",
    )

    cfg = load_config(cfg_file)

    assert cfg.data_dir == tmp_path / "connector-data"
    assert stat.S_IMODE(os.stat(cfg.data_dir).st_mode) == 0o700


def test_existing_data_dir_is_tightened_to_private(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    os.chmod(data_dir, 0o755)
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {data_dir}\npublic_url: https://vault.example.com\n",
    )

    load_config(cfg_file)

    assert stat.S_IMODE(os.stat(data_dir).st_mode) == 0o700


def test_missing_public_url_raises(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n",
    )

    with pytest.raises(ValueError, match="public_url"):
        load_config(cfg_file)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_default_path_uses_user_dir_not_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    _write(
        overlay / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://vault.example.com\n",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("USER_DIR", str(overlay))
    monkeypatch.chdir(elsewhere)

    cfg = load_config()

    assert cfg.public_url == "https://vault.example.com"


def test_relative_path_resolves_against_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    _write(
        overlay / "other.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://other.example.com\n",
    )
    monkeypatch.setenv("USER_DIR", str(overlay))
    monkeypatch.chdir(tmp_path)

    assert load_config(Path("other.yaml")).public_url == "https://other.example.com"


def test_user_dir_defaults_to_repo_user_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USER_DIR", raising=False)

    assert user_dir() == REPO_ROOT / "user"
    assert (REPO_ROOT / "pyproject.toml").is_file()


def test_user_dir_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USER_DIR", "~/overlay")

    assert user_dir() == tmp_path / "overlay"


def test_port_default(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://vault.example.com\n",
    )

    assert load_config(cfg_file).port == 8788


def test_port_from_file(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://vault.example.com\nport: 9100\n",
    )

    assert load_config(cfg_file).port == 9100


@pytest.mark.parametrize("value", ["'8788'", "0", "70000", "true"])
def test_bad_port_raises(tmp_path: Path, value: str) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        f"public_url: https://vault.example.com\nport: {value}\n",
    )

    with pytest.raises(ValueError, match="port"):
        load_config(cfg_file)


def test_bad_timezone_raises(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path / 'Vault'}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://vault.example.com\ntimezone: Europe/Nowhere\n",
    )

    with pytest.raises(ValueError, match="timezone"):
        load_config(cfg_file)


def test_layout_defaults_without_a_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    monkeypatch.setenv("USER_DIR", str(overlay))
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://vault.example.com\n",
    )

    assert load_config(cfg_file).layout == VaultLayout()


def test_layout_is_read_from_the_profile_in_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    _write(overlay / "profile.yaml", "notes:\n  actions_dir: Open\n")
    monkeypatch.setenv("USER_DIR", str(overlay))
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        "public_url: https://vault.example.com\n",
    )

    assert load_config(cfg_file).layout == VaultLayout(actions_dir="Open")


_BASE = "public_url: https://vault.example.com\n"


def _config_file(tmp_path: Path, extra: str = "") -> Path:
    return _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n{_BASE}{extra}",
    )


def test_integrations_default_off(tmp_path: Path) -> None:
    integrations = load_config(_config_file(tmp_path)).integrations

    assert integrations == Integrations()
    assert integrations.enabled_names() == ()


def test_integrations_parse(tmp_path: Path) -> None:
    cfg = load_config(
        _config_file(
            tmp_path,
            "integrations:\n  reminders: eventkit\n  mail: off\n"
            "  contacts: carddav\n  calendar: caldav\n",
        )
    )

    assert cfg.integrations == Integrations(
        reminders=True, calendar=True, mail=False, contacts=True
    )
    assert cfg.integrations.enabled_names() == ("reminders", "calendar", "contacts")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("calendar", "google"),
        ("reminders", "caldav"),
        ("mail", "true"),
        ("contacts", "eventkit"),
    ],
)
def test_integrations_bad_backend_raises(tmp_path: Path, key: str, value: str) -> None:
    cfg_file = _config_file(tmp_path, f"integrations:\n  {key}: {value}\n")

    with pytest.raises(ValueError, match=key):
        load_config(cfg_file)


def test_integrations_unknown_key_raises(tmp_path: Path) -> None:
    cfg_file = _config_file(tmp_path, "integrations:\n  photos: icloud\n")

    with pytest.raises(ValueError, match="photos"):
        load_config(cfg_file)


@pytest.mark.parametrize("old_key", ["alert_list_household", "alert_list_child"])
def test_old_alert_list_keys_refused(tmp_path: Path, old_key: str) -> None:
    cfg_file = _config_file(tmp_path, f"{old_key}: X\n")

    with pytest.raises(ValueError, match=rf"{old_key}.*alert_lists"):
        load_config(cfg_file)


def test_empty_household_name_raises(tmp_path: Path) -> None:
    cfg_file = _config_file(tmp_path, "alert_lists:\n  household: ''\n")

    with pytest.raises(ValueError, match=r"alert_lists\.household must be a non-empty string"):
        load_config(cfg_file)


def test_instructions_overlay_is_in_user_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    monkeypatch.setenv("USER_DIR", str(overlay))
    built = ConnectorConfig(
        vault_path=tmp_path,
        data_dir=tmp_path / "data",
        public_url="https://vault.example.com",
        allowed_redirect_uris=(),
    )

    assert load_config(_config_file(tmp_path)).instructions_overlay == (
        overlay / "connector_instructions.yaml"
    )
    assert built.instructions_overlay == overlay / "connector_instructions.yaml"


def _minimal(tmp_path: Path, extra: str = "") -> Path:
    return _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        f"public_url: https://vault.example.com\n{extra}",
    )


def test_the_zone_comes_from_the_profile_when_the_connector_sets_none(
    tmp_path: Path, private_overlay_is_a_temp_folder: Path
) -> None:
    _write(private_overlay_is_a_temp_folder / "profile.yaml", "user:\n  timezone: America/New_York\n")

    cfg = load_config(_minimal(tmp_path))

    assert cfg.timezone == "America/New_York"
    assert cfg.timezone_source == "profile.yaml"


def test_the_connector_zone_wins_over_the_profile(
    tmp_path: Path, private_overlay_is_a_temp_folder: Path
) -> None:
    _write(private_overlay_is_a_temp_folder / "profile.yaml", "user:\n  timezone: America/New_York\n")

    cfg = load_config(_minimal(tmp_path, "timezone: Asia/Tokyo\n"))

    assert cfg.timezone == "Asia/Tokyo"
    assert cfg.timezone_source == "vault_connector.yaml"


def test_a_bad_profile_zone_is_refused(
    tmp_path: Path, private_overlay_is_a_temp_folder: Path
) -> None:
    _write(private_overlay_is_a_temp_folder / "profile.yaml", "user:\n  timezone: Mars/Olympus\n")

    with pytest.raises(ValueError, match="user.timezone"):
        load_config(_minimal(tmp_path))


def test_the_mac_zone_is_the_tz_variable_when_it_names_a_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert system_timezone() == "Asia/Tokyo"


def test_a_tz_value_without_a_slash_still_names_a_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.brain.connector import config as config_module

    link = tmp_path / "localtime"
    link.symlink_to("/var/db/timezone/zoneinfo/America/Chicago")
    monkeypatch.setattr(config_module, "LOCALTIME", link)
    monkeypatch.setenv("TZ", "UTC")
    assert system_timezone() == "UTC"
    monkeypatch.setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3")
    assert system_timezone() == "America/Chicago"


def test_the_mac_zone_is_read_from_the_localtime_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.brain.connector import config as config_module

    link = tmp_path / "localtime"
    link.symlink_to("/var/db/timezone/zoneinfo/America/Chicago")
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(config_module, "LOCALTIME", link)
    assert system_timezone() == "America/Chicago"
    monkeypatch.setattr(config_module, "LOCALTIME", tmp_path / "missing")
    assert system_timezone() == "UTC"


def test_the_shipped_example_leaves_the_zone_to_the_profile(
    tmp_path: Path, private_overlay_is_a_temp_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    overlay = private_overlay_is_a_temp_folder
    example = REPO_ROOT / "examples" / "vault_connector.example.yaml"
    _write(overlay / "vault_connector.yaml", example.read_text(encoding="utf-8"))
    _write(overlay / "profile.yaml", "user:\n  timezone: America/New_York\n")

    cfg = load_config()

    assert (cfg.timezone, cfg.timezone_source) == ("America/New_York", "profile.yaml")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("alert_lists: Family\n", "alert_lists must be a mapping"),
        ("alert_lists:\n  househld: List A\n", "alert_lists has an unknown key househld"),
        ("integrations: [reminders]\n", "integrations must be a mapping"),
    ],
)
def test_a_badly_shaped_alert_or_integrations_section_is_refused(
    tmp_path: Path, extra: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(_config_file(tmp_path, extra))


def test_the_zone_is_the_macs_when_neither_file_sets_one(
    tmp_path: Path, private_overlay_is_a_temp_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    cfg = load_config(_minimal(tmp_path))

    assert (cfg.timezone, cfg.timezone_source) == ("Asia/Tokyo", "this Mac")


def test_a_plain_http_public_url_is_refused(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "vault_connector.yaml",
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\npublic_url: http://vault.example.com\n",
    )

    with pytest.raises(ValueError, match="public_url must start with https://"):
        load_config(cfg_file)
