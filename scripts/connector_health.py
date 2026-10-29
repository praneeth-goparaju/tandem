"""Check that the vault connector answers, and raise a reminder when it does not.

Run from launchd every 5 minutes:

    .venv/bin/python scripts/connector_health.py

It asks ``<public_url>/health``. After two failed checks in a row it creates one
reminder through the notify helper; a good check clears the count.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from src.brain.atomic_io import atomic_write_text, read_regular_text  # noqa: E402
from src.brain.connector.config import ConnectorConfig, load_config  # noqa: E402
from src.brain.connector.notify import notify  # noqa: E402

ALERT_TITLE = "Vault connector is down"
STATE_FILE = "health.json"
FAILURES_BEFORE_ALERT = 2
REQUEST_TIMEOUT_SECONDS = 10.0
_STATE_MODE = 0o600
_REASON_BODY_CHARS = 80

Fetch = Callable[[str], tuple[int, str]]
NotifyFn = Callable[..., dict[str, Any]]


def fetch_health(url: str) -> tuple[int, str]:
    """GET ``url`` and return the status code and body. Redirects are not followed."""
    response = httpx.get(
        url, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    )
    return response.status_code, response.text


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(read_regular_text(path))
    except (OSError, ValueError):
        return {"failures": 0, "alerted": False}
    failures = raw.get("failures") if isinstance(raw, dict) else None
    alerted = raw.get("alerted") if isinstance(raw, dict) else None
    return {
        "failures": failures if isinstance(failures, int) and failures >= 0 else 0,
        "alerted": alerted is True,
    }


def _write_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(state), mode=_STATE_MODE)


def _failure_reason(fetch: Fetch, url: str) -> str | None:
    """None when the connector is healthy, else a short plain reason."""
    try:
        status, body = fetch(url)
    except Exception as exc:  # noqa: BLE001
        return f"No answer from {url}: {str(exc) or type(exc).__name__}"
    if status == 200 and body.strip() == "ok":
        return None
    return f"{url} answered {status}: {body.strip()[:_REASON_BODY_CHARS]}"


def run_check(
    config: ConnectorConfig, fetch: Fetch, notify_fn: NotifyFn, now: datetime
) -> dict[str, Any]:
    """One health check. Returns the new state, ``{"failures": int, "alerted": bool}``."""
    path = config.data_dir / STATE_FILE
    state = _read_state(path)
    reason = _failure_reason(fetch, f"{config.public_url.rstrip('/')}/health")
    if reason is None:
        state = {"failures": 0, "alerted": False}
    else:
        state["failures"] += 1
        if state["failures"] >= FAILURES_BEFORE_ALERT and not state["alerted"]:
            sent = notify_fn(
                ALERT_TITLE,
                config.alert_lists.household,
                notes=reason,
                now=now,
                enabled=config.integrations.reminders,
                timezone=config.timezone,
            )
            state["alerted"] = bool(sent.get("ok"))
    _write_state(path, state)
    return state


def main() -> int:
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        print(f"Cannot read the connector settings: {exc}", file=sys.stderr)
        return 2
    run_check(config, fetch_health, notify, datetime.now(UTC))
    return 0


if __name__ == "__main__":
    sys.exit(main())
