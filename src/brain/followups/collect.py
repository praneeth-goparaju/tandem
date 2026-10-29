from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

from src.brain.followups.digest import (
    load_state,
    render_digest,
    save_state,
    write_digest,
    write_index,
)
from src.brain.followups.entries import Entry
from src.brain.followups.slack_history import (
    ChannelCursor,
    ChannelRole,
    SlackBudget,
    channel_roles,
    read_channel,
    speakers_from,
)
from src.brain.followups.transcripts import iter_transcript_entries
from src.brain.runtime.loader import load_integrations, load_yaml, resolve_user_dir
from src.db import expire_follow_ups, record_event

log = logging.getLogger("brain.followups.collect")

REPO_ROOT = Path(__file__).resolve().parents[3]
_FIRST_RUN_DAYS = 14
_HISTORY_WINDOW_DAYS = 14
_EXPIRE_AFTER_DAYS = 14
_THREAD_READS = 40
_SLACK_SECONDS = 90.0
_ENV_FILE_KEYS = ("SLACK_BOT_TOKEN", "BRAIN_SLACK_OFF", "OWNER_SLACK_USER_ID")


def load_config(user_dir: Path, repo_root: Path) -> dict[str, Any]:
    config = {
        **load_yaml(repo_root / "config" / "followups.yaml"),
        **load_yaml(user_dir / "followups.yaml"),
    }
    excluded = config.setdefault("exclude_projects", [])
    if not isinstance(excluded, list) or not all(isinstance(name, str) for name in excluded):
        raise ValueError("followups.yaml: exclude_projects must be a list of folder names")
    paths = config.setdefault("exclude_paths", [])
    if not isinstance(paths, list) or not all(
        isinstance(name, str) and name.strip() and "/" not in name for name in paths
    ):
        raise ValueError(
            "followups.yaml: exclude_paths must be a list of folder names, one path component each"
        )
    return config


async def collect(
    *,
    user_dir: Path,
    repo_root: Path,
    projects_dir: Path,
    home: Path,
    now: datetime,
    client_factory: Callable[[], Any] | None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    deadline = clock() + _SLACK_SECONDS
    config = load_config(user_dir, repo_root)
    folder = user_dir / "followups"
    committed = _committed(load_state(folder))
    first_day = now - timedelta(days=_FIRST_RUN_DAYS)
    since = _saved_time(committed.get("transcripts")) or first_day
    starts = _slack_cursors(committed.get("slack"))

    transcripts = list(
        iter_transcript_entries(
            projects_dir,
            since=since,
            until=now,
            home=home,
            exclude_projects=frozenset(config["exclude_projects"]),
            exclude_paths=frozenset(name.strip() for name in config["exclude_paths"]),
        )
    )
    integrations = load_integrations(repo_root / "config", user_dir)
    roles = channel_roles(integrations, config, today=now.date())
    slack, reached, failed, unfinished = await _slack_entries(
        roles,
        client_factory,
        integrations=integrations,
        starts=starts,
        first=ChannelCursor(_slack_ts(first_day)),
        now_ts=_slack_ts(now),
        budget=SlackBudget(replies_left=_THREAD_READS, deadline=deadline, clock=clock),
    )

    entries = [*transcripts, *slack]
    text = render_digest(entries)
    write_digest(folder, text, today=now.date())
    write_index(
        folder,
        {entry.ref: {"source": entry.source, "place": entry.place} for entry in entries},
        today=now.date(),
    )
    pending = {
        "slack": {
            channel: _saved_cursor(read) for channel, read in sorted({**starts, **reached}.items())
        },
        "transcripts": now.isoformat(),
    }
    save_state(folder, {"committed": committed, "pending": pending})
    return {
        "slack": len(slack),
        "claude_code": len(transcripts),
        "bytes": len(text.encode("utf-8")),
        "expired": expire_follow_ups(_EXPIRE_AFTER_DAYS, now=now),
        "slack_errors": failed,
        "slack_unfinished": unfinished,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        _load_env_file(REPO_ROOT / ".env")
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        home = Path.home()
        counts = asyncio.run(
            collect(
                user_dir=resolve_user_dir(REPO_ROOT),
                repo_root=REPO_ROOT,
                projects_dir=home / ".claude" / "projects",
                home=home,
                now=datetime.now(UTC),
                client_factory=(lambda: _slack_client(token)) if token else None,
            )
        )
        detail = json.dumps(counts, sort_keys=True)
        record_event(actor="followups:collector", action="followups_collected", detail=detail)
        print(detail)
    except Exception:
        log.exception("follow-up collection failed")
        return 1
    return 0


async def _slack_entries(
    roles: list[ChannelRole],
    client_factory: Callable[[], Any] | None,
    *,
    integrations: dict[str, Any],
    starts: dict[str, ChannelCursor],
    first: ChannelCursor,
    now_ts: str,
    budget: SlackBudget,
) -> tuple[list[Entry], dict[str, ChannelCursor], int, int]:
    if not roles or client_factory is None or _slack_off():
        return [], {}, 0, 0
    client = client_factory()
    who = speakers_from(
        integrations,
        owner=os.environ.get("OWNER_SLACK_USER_ID", "").strip(),
        bot_id=await _our_bot_id(client, budget),
    )
    entries: list[Entry] = []
    reached: dict[str, ChannelCursor] = {}
    failed = unfinished = 0
    for role in roles:
        start = starts.get(role.channel_id, first)
        try:
            read = await read_channel(
                client,
                role,
                start,
                now_ts=now_ts,
                window_days=_HISTORY_WINDOW_DAYS,
                budget=budget,
                permalink=_permalink,
                speakers=who,
            )
        except Exception:
            log.exception("Slack history failed for channel %s", role.channel_id)
            reached[role.channel_id] = start
            failed += 1
            continue
        entries += read.entries
        reached[role.channel_id] = read.cursor
        unfinished += not read.finished
    return entries, reached, failed, unfinished


async def _our_bot_id(client: Any, budget: SlackBudget) -> str | None:
    try:
        response = await budget.call(client.auth_test, replies=False)
        bot_id = response.get("bot_id")
    except Exception:
        log.warning("Slack auth.test failed; every bot is named by its own id")
        return None
    return bot_id if isinstance(bot_id, str) else None


def _slack_client(token: str) -> AsyncWebClient:
    return AsyncWebClient(
        token=token, retry_handlers=[AsyncRateLimitErrorRetryHandler(max_retry_count=1)]
    )


def _load_env_file(path: Path) -> None:
    values = dotenv_values(path)
    for key in _ENV_FILE_KEYS:
        value = values.get(key)
        if value and key not in os.environ:
            os.environ[key] = value


def _slack_off() -> bool:
    return os.environ.get("BRAIN_SLACK_OFF", "").strip().lower() in {"1", "true", "yes", "on"}


def _permalink(channel_id: str, ts: str) -> str:
    return f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def _slack_ts(moment: datetime) -> str:
    return f"{moment.timestamp():.6f}"


def _saved_ts(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        Decimal(value)
    except InvalidOperation:
        return None
    return value


def _saved_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _committed(state: dict[str, Any]) -> dict[str, Any]:
    committed = state.get("committed")
    return committed if isinstance(committed, dict) else {}


def _slack_cursors(value: object) -> dict[str, ChannelCursor]:
    if not isinstance(value, dict):
        return {}
    cursors: dict[str, ChannelCursor] = {}
    for channel, saved in value.items():
        loaded = _loaded_cursor(saved) if isinstance(channel, str) else None
        if loaded is not None:
            cursors[channel] = loaded
    return cursors


def _loaded_cursor(saved: object) -> ChannelCursor | None:
    if not isinstance(saved, dict):
        return None
    oldest = _saved_ts(saved.get("oldest"))
    if oldest is None:
        return None
    until = _saved_ts(saved.get("until"))
    reached = _saved_ts(saved.get("reached"))
    if until is None or Decimal(until) < Decimal(oldest):
        return ChannelCursor(oldest)
    return ChannelCursor(oldest, until=until, reached=reached)


def _saved_cursor(cursor: ChannelCursor) -> dict[str, str]:
    saved = {"oldest": cursor.oldest}
    if cursor.until is not None:
        saved["until"] = cursor.until
    if cursor.reached is not None:
        saved["reached"] = cursor.reached
    return saved


if __name__ == "__main__":
    sys.exit(main())
