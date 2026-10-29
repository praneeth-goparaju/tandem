import os
from pathlib import Path

from dotenv import load_dotenv

from src.brain.atomic_io import read_regular_text

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_repo_env(repo_root: Path = REPO_ROOT) -> bool:
    """Load ``<repo_root>/.env`` and nothing else.

    Never a parent folder's ``.env``: a worktree inside another checkout must
    not pick up that checkout's settings and secrets.
    """
    return load_dotenv(repo_root / ".env")


load_repo_env()


def _expanded_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


_default_vault = Path.home() / "Documents" / "notes"
VAULT_PATH = _expanded_path(os.getenv("VAULT_PATH", str(_default_vault)))

AGENT_NOTES_DIR = _expanded_path(
    os.getenv("AGENT_NOTES_DIR", str(VAULT_PATH / "Agent Notes"))
)

MEMORY_DB_PATH = _expanded_path(
    os.getenv("MEMORY_DB_PATH")
    or os.getenv("VAULT_DB_PATH")
    or str(Path(__file__).parent.parent.parent / "search" / "data" / "vault.db")
)

GARMIN_TOKENSTORE = _expanded_path(
    os.getenv("GARMIN_TOKENSTORE", str(Path.home() / ".garminconnect"))
)
GARMIN_ENABLED = os.getenv("GARMIN_ENABLED", "").strip().lower() in {
    "1", "true", "yes", "on",
}


def agents_paused() -> bool:
    """True when BRAIN_AGENTS_PAUSED turns every agent run off.

    The daemon then runs only its services: the action executor, the reply
    listener's approvals, and the code jobs. Read at call time, not at import,
    so tests and a daemon restart always see the current value.
    """
    return os.getenv("BRAIN_AGENTS_PAUSED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }

BRIEF_CHANNEL_ID = os.getenv("BRIEF_CHANNEL_ID", "")

try:
    REPLY_AGENT_TIMEOUT = int(os.getenv("BRAIN_REPLY_TIMEOUT", "180"))
except ValueError:
    REPLY_AGENT_TIMEOUT = 180


def _load_profile() -> dict:
    import yaml
    p = _expanded_path(
        os.getenv("USER_DIR", str(Path(__file__).resolve().parent.parent.parent / "user"))
    ) / "profile.yaml"
    try:
        return yaml.safe_load(read_regular_text(p)) or {}
    except Exception:  # noqa: BLE001
        return {}


USER_PROFILE = _load_profile()
USER_NAME = (USER_PROFILE.get("user") or {}).get("name") or "owner"


class _HousingCorridor:
    __slots__ = ("_data",)

    def __init__(self, data: dict | None) -> None:
        self._data = data or {}

    def _require(self) -> dict:
        if not self._data:
            raise RuntimeError(
                "housing_corridor not configured in user/profile.yaml — "
                "housing search is opt-in. Add a `housing_corridor` block "
                "with anchor/radius_km/cities/postcodes to enable."
            )
        return self._data

    @property
    def configured(self) -> bool:
        return bool(self._data)

    @property
    def anchor(self) -> tuple[float, float, int]:
        d = self._require()
        a = d.get("anchor") or {}
        return float(a["lat"]), float(a["lon"]), int(d.get("radius_km", 20))

    @property
    def cities(self) -> list[str]:
        return list(self._require().get("cities") or [])

    @property
    def postcodes(self) -> set[str]:
        return {str(p) for p in (self._require().get("postcodes") or [])}

    @property
    def city_aliases(self) -> set[str]:
        d = self._require()
        out: set[str] = set()
        for v in (d.get("city_aliases") or {}).values():
            for alias in v or []:
                out.add(str(alias).lower())
        for c in d.get("cities") or []:
            out.add(str(c).lower())
        return out

    @property
    def kleinanzeigen_location_id(self) -> str:
        return str(self._require().get("kleinanzeigen_location_id") or "")

    @property
    def kleinanzeigen_radius_km(self) -> int:
        return int(self._require().get("kleinanzeigen_radius_km", 15))

    @property
    def kleinanzeigen_city_slugs(self) -> dict[str, str]:
        return {
            str(k).lower(): str(v)
            for k, v in (self._require().get("kleinanzeigen_city_slugs") or {}).items()
        }

    @property
    def immowelt_default_radius_km(self) -> int:
        return int(self._require().get("immowelt_default_radius_km", 15))

    @property
    def immowelt_geo_ids(self) -> dict[str, str]:
        return {
            str(k).lower(): str(v).upper()
            for k, v in (self._require().get("immowelt_geo_ids") or {}).items()
        }

    @property
    def city_alias_groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for k, v in (self._require().get("city_aliases") or {}).items():
            out[str(k).lower()] = [str(a).lower() for a in (v or [])]
        return out

    @property
    def label(self) -> str:
        a = (self._data.get("anchor") or {}) if self._data else {}
        return str(a.get("label") or "")

    def description(self) -> str:
        if not self._data:
            return "configured corridor"
        cities = self._data.get("cities") or []
        if not cities:
            return self.label or "configured corridor"
        if len(cities) <= 3:
            return " / ".join(cities)
        return f"{cities[0]} + {len(cities) - 1} nearby"


HOUSING_CORRIDOR = _HousingCorridor(USER_PROFILE.get("housing_corridor"))

TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "")

ICLOUD_APPLE_ID = os.getenv("ICLOUD_APPLE_ID", "")
ICLOUD_APP_PASSWORD = os.getenv("ICLOUD_APP_PASSWORD", "")
# IMAP-specific username override. iCloud's IMAP server wants the actual
# @icloud.com / @me.com / @mac.com mailbox identifier, not the Apple ID
# (which can be a non-Apple domain like @outlook.com). CalDAV / CardDAV
# accept the Apple ID directly; IMAP doesn't. Set this only when the
# Apple ID and the iCloud Mail address differ. Falls back to Apple ID.
ICLOUD_IMAP_USER = os.getenv("ICLOUD_IMAP_USER", "")
# Comma-separated list of YOUR known send-from identities — primary
# iCloud address, custom-domain aliases, traditional iCloud aliases.
# Used by `mail_reply_link` to detect HMA threads: any recipient address
# on @icloud.com / @me.com / @mac.com that is NOT in this list is
# treated as a Hide-My-Email alias and the tool branches into a
# text-only Slack push (mailto: can't set From to an HMA, and HMAs
# aren't selectable in Mail.app's compose From dropdown — the user has
# to use Mail.app's native Reply on the parent thread, where Apple's
# relay does the From rewrite). HMAs auto-generated by third parties
# (someone@icloud.com) and Sign-in-with-Apple HMAs (@privaterelay.appleid.com)
# do NOT belong in this list.
ICLOUD_FROM_ALIASES = [
    a.strip() for a in os.getenv("ICLOUD_FROM_ALIASES", "").split(",") if a.strip()
]

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

USER_DIR = _expanded_path(os.getenv("USER_DIR", str(REPO_ROOT / "user")))

OCCASIONS_DIR = _expanded_path(
    os.getenv("OCCASIONS_DIR", str(USER_DIR / "occasions"))
)
OCCASIONS_PEOPLE = OCCASIONS_DIR / "people.yaml"
OCCASIONS_FESTIVALS = OCCASIONS_DIR / "festivals.yaml"
OCCASIONS_VOICE = OCCASIONS_DIR / "voice.yaml"

TIMEZONE = os.getenv("TIMEZONE", "UTC")
