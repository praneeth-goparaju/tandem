"""Settings for the vault connector, read from the private overlay.

The deployment values (vault path, public hostname, alert list name) live only
in the gitignored ``user/vault_connector.yaml``. The shipped example is
``examples/vault_connector.example.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from src.brain.connector.layout import DEFAULT_LAYOUT, VaultLayout, load_layout
from src.brain.runtime.loader import load_yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_CONFIG_PATH = Path("vault_connector.yaml")
DEFAULT_DATA_DIR = "~/connector-data"
DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
DEFAULT_PORT = 8788
PROFILE_NAME = "profile.yaml"
INSTRUCTIONS_OVERLAY_NAME = "connector_instructions.yaml"
LOCALTIME = Path("/etc/localtime")
FALLBACK_TIMEZONE = "UTC"
_PRIVATE_DIR_MODE = 0o700
_OLD_ALERT_KEY_PREFIX = "alert_list_"
_INTEGRATION_BACKENDS = {
    "reminders": "eventkit",
    "calendar": "caldav",
    "mail": "imap",
    "contacts": "carddav",
}


@dataclass(frozen=True)
class AlertLists:
    household: str = "Family"


@dataclass(frozen=True)
class Integrations:
    reminders: bool = False
    calendar: bool = False
    mail: bool = False
    contacts: bool = False

    def enabled_names(self) -> tuple[str, ...]:
        return tuple(name for name in _INTEGRATION_BACKENDS if getattr(self, name))


@dataclass(frozen=True)
class ConnectorConfig:
    vault_path: Path
    data_dir: Path
    public_url: str
    allowed_redirect_uris: tuple[str, ...]
    alert_lists: AlertLists = AlertLists()
    integrations: Integrations = Integrations()
    timezone: str = FALLBACK_TIMEZONE
    timezone_source: str = "default"
    port: int = DEFAULT_PORT
    layout: VaultLayout = DEFAULT_LAYOUT
    instructions_overlay: Path = field(
        default_factory=lambda: user_dir() / INSTRUCTIONS_OVERLAY_NAME
    )
    profile_path: Path | None = None


def _required_text(raw: dict[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {key} is required")
    return value.strip()


def _optional_text(raw: dict[str, Any], key: str, default: str, path: Path) -> str:
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {key} must be a non-empty string")
    return value.strip()


def _alert_lists(raw: dict[str, Any], path: Path) -> AlertLists:
    for key in raw:
        if str(key).startswith(_OLD_ALERT_KEY_PREFIX):
            raise ValueError(
                f"{path}: {key} is no longer used; "
                "write alert_lists: {household: <list name>} instead"
            )
    section = raw.get("alert_lists")
    if section is None:
        return AlertLists()
    if not isinstance(section, dict):
        raise ValueError(f"{path}: alert_lists must be a mapping")
    unknown = sorted(str(key) for key in section if key != "household")
    if unknown:
        raise ValueError(f"{path}: alert_lists has an unknown key {unknown[0]}")
    household = section.get("household")
    if household is None:
        return AlertLists()
    if not isinstance(household, str) or not household.strip():
        raise ValueError(f"{path}: alert_lists.household must be a non-empty string")
    return AlertLists(household=household.strip())


def _integrations(raw: dict[str, Any], path: Path) -> Integrations:
    section = raw.get("integrations")
    if section is None:
        return Integrations()
    if not isinstance(section, dict):
        raise ValueError(f"{path}: integrations must be a mapping")
    unknown = sorted(str(key) for key in section if key not in _INTEGRATION_BACKENDS)
    if unknown:
        raise ValueError(f"{path}: integrations has an unknown key {unknown[0]}")
    switches: dict[str, bool] = {}
    for name, backend in _INTEGRATION_BACKENDS.items():
        value = section.get(name, "off")
        if value is False:
            value = "off"  # YAML reads an unquoted off as false
        if value not in (backend, "off"):
            raise ValueError(f"{path}: integrations.{name} must be {backend} or off")
        switches[name] = value == backend
    return Integrations(**switches)


def _port(raw: dict[str, Any], path: Path) -> int:
    value = raw.get("port", DEFAULT_PORT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{path}: port must be a number from 1 to 65535")
    return value


def _known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def system_timezone() -> str:
    """The Mac's own zone: ``TZ`` when it names one, else ``/etc/localtime``'s target, else UTC."""
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        candidates.append(os.readlink(LOCALTIME).rpartition("zoneinfo/")[2])
    except OSError:
        pass
    for name in candidates:
        if name and _known_zone(name):
            return name
    return FALLBACK_TIMEZONE


def resolve_timezone(raw: dict[str, Any], path: Path, profile_path: Path) -> tuple[str, str]:
    """The connector's zone and where it came from: ``vault_connector.yaml``,
    ``profile.yaml`` or ``this Mac``."""
    name = _optional_text(raw, "timezone", "", path)
    if name:
        if not _known_zone(name):
            raise ValueError(f"{path}: timezone {name!r} is not a known time zone")
        return name, "vault_connector.yaml"
    profile = load_yaml(profile_path) if profile_path.is_file() else {}
    user = profile.get("user") if isinstance(profile, dict) else None
    zone = user.get("timezone") if isinstance(user, dict) else None
    if zone is not None:
        if not isinstance(zone, str) or not _known_zone(zone.strip()):
            raise ValueError(f"{profile_path}: user.timezone {zone!r} is not a known time zone")
        return zone.strip(), PROFILE_NAME
    return system_timezone(), "this Mac"


def _redirect_uris(raw: dict[str, Any], path: Path) -> tuple[str, ...]:
    value = raw.get("allowed_redirect_uris")
    if value is None:
        return DEFAULT_REDIRECT_URIS
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ValueError(f"{path}: allowed_redirect_uris must be a list of URLs")
    return tuple(item.strip() for item in value)


def _private_dir(directory: Path) -> Path:
    directory.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    os.chmod(directory, _PRIVATE_DIR_MODE)
    return directory


def user_dir() -> Path:
    """The private overlay folder: ``USER_DIR`` if set, else ``<repo root>/user``."""
    return Path(os.getenv("USER_DIR") or REPO_ROOT / "user").expanduser()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ConnectorConfig:
    """Read the connector settings.

    A relative path (including the default) is looked up in the private overlay
    folder, never in the current directory, so the result does not depend on
    where the process was started.
    """
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = user_dir() / path
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    vault_path = Path(_required_text(raw, "vault_path", path)).expanduser()
    public_url = _required_text(raw, "public_url", path)
    if not public_url.startswith("https://"):
        raise ValueError(f"{path}: public_url must start with https://")
    data_dir = Path(_optional_text(raw, "data_dir", DEFAULT_DATA_DIR, path)).expanduser()
    if not data_dir.is_absolute():
        data_dir = path.parent.resolve() / data_dir
    data_dir = _private_dir(data_dir)
    timezone, timezone_source = resolve_timezone(raw, path, user_dir() / PROFILE_NAME)

    return ConnectorConfig(
        vault_path=vault_path,
        data_dir=data_dir,
        public_url=public_url,
        allowed_redirect_uris=_redirect_uris(raw, path),
        alert_lists=_alert_lists(raw, path),
        integrations=_integrations(raw, path),
        timezone=timezone,
        timezone_source=timezone_source,
        port=_port(raw, path),
        layout=load_layout(user_dir() / PROFILE_NAME),
        instructions_overlay=user_dir() / INSTRUCTIONS_OVERLAY_NAME,
        profile_path=user_dir() / PROFILE_NAME,
    )
