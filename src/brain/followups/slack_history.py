from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from slack_sdk.errors import SlackApiError

from src.brain.followups.entries import Entry

log = logging.getLogger("brain.followups.slack")

MORNING_MESSAGE_EVENT = "morning_message"

_PAGE_SIZE = 200
_DAY_SECONDS = 86_400
_PLACES = (
    ("alfred", "slack_shared", "shared"),
    ("alfred_private", "slack_private", "private"),
)
_PEOPLE_SUBTYPES = (None, "thread_broadcast", "file_share")
_BOT_SUBTYPES = (None, "bot_message", "thread_broadcast")


@dataclass(frozen=True)
class ChannelRole:
    channel_id: str
    source: str
    place: str


@dataclass(frozen=True)
class Speakers:
    owner: str | None = None
    co_owners: frozenset[str] = frozenset()
    bot_id: str | None = None

    def person(self, user: str) -> str:
        if self.owner and user == self.owner:
            return "owner"
        return "co-owner" if user in self.co_owners else f"user:{user}"

    def bot(self, bot_id: str) -> str:
        return "alfred" if self.bot_id and bot_id == self.bot_id else f"bot:{bot_id}"


@dataclass(frozen=True)
class _Reading:
    role: ChannelRole
    window: tuple[Decimal, Decimal]
    permalink: Callable[[str, str], str]
    speakers: Speakers


@dataclass(frozen=True)
class ChannelCursor:
    oldest: str
    until: str | None = None
    reached: str | None = None


@dataclass(frozen=True)
class ChannelRead:
    entries: list[Entry]
    cursor: ChannelCursor
    finished: bool


class OutOfBudget(Exception):
    ...


@dataclass
class SlackBudget:
    replies_left: int
    deadline: float
    clock: Callable[[], float] = time.monotonic
    exhausted: bool = False

    async def call(
        self, method: Callable[..., Awaitable[Any]], *, replies: bool, **params: Any
    ) -> Any:
        if self.exhausted or (replies and self.replies_left <= 0):
            raise OutOfBudget
        left = self.deadline - self.clock()
        if left <= 0:
            self.exhausted = True
            raise OutOfBudget
        if replies:
            self.replies_left -= 1
        try:
            return await asyncio.wait_for(method(**params), timeout=left)
        except TimeoutError:
            self.exhausted = True
            raise OutOfBudget from None
        except SlackApiError as exc:
            if _rate_limited(exc):
                self.exhausted = True
                raise OutOfBudget from exc
            raise


async def read_channel(
    client: Any,
    role: ChannelRole,
    cursor: ChannelCursor,
    *,
    now_ts: str,
    window_days: int,
    budget: SlackBudget,
    permalink: Callable[[str, str], str],
    speakers: Speakers,
) -> ChannelRead:
    """Read one channel on from `cursor` as far as `budget` allows.

    A thread that fails is logged and skipped; a history call that fails raises.
    """
    found: dict[str, Entry] = {}
    pass_ = (window_days, budget, permalink, speakers)
    if cursor.until is not None:
        read = await _read_pass(client, role, cursor, cursor.until, found, *pass_)
        if not read.finished:
            return read
        cursor = read.cursor
    return await _read_pass(client, role, cursor, now_ts, found, *pass_)


async def _read_pass(
    client: Any,
    role: ChannelRole,
    cursor: ChannelCursor,
    until: str,
    found: dict[str, Entry],
    window_days: int,
    budget: SlackBudget,
    permalink: Callable[[str, str], str],
    speakers: Speakers,
) -> ChannelRead:
    reading = _Reading(role, (Decimal(cursor.oldest), Decimal(until)), permalink, speakers)
    start = cursor.reached or format(reading.window[0] - window_days * _DAY_SECONDS, "f")
    try:
        history = [
            message
            async for message in _messages(
                budget,
                client.conversations_history,
                replies=False,
                channel=role.channel_id,
                oldest=start,
            )
        ]
    except OutOfBudget:
        return ChannelRead(_ordered(found), cursor, finished=False)
    texts = {message["ts"]: message.get("text") for message in history}
    reached = cursor.reached
    for message in sorted(history, key=lambda item: Decimal(item["ts"])):
        ts = message["ts"]
        if Decimal(ts) > reading.window[1]:
            break
        if reached is not None and Decimal(ts) <= Decimal(reached):
            continue
        kept: dict[str, Entry] = {}
        _keep(kept, message, reading, _thread_text(message, texts.get(message.get("thread_ts"))))
        if _may_have_new_replies(message, reading.window[0]):
            try:
                thread = _messages(
                    budget,
                    client.conversations_replies,
                    replies=True,
                    channel=role.channel_id,
                    ts=ts,
                    oldest=cursor.oldest,
                )
                async for reply in thread:
                    _keep(kept, reply, reading, _thread_text(reply, message.get("text")))
            except OutOfBudget:
                stopped = (
                    cursor
                    if reached == cursor.reached
                    else ChannelCursor(cursor.oldest, until=until, reached=reached)
                )
                return ChannelRead(_ordered(found), stopped, finished=False)
            except Exception:
                log.exception("Slack thread %s in channel %s failed; skipped", ts, role.channel_id)
        found.update(kept)
        reached = ts
    return ChannelRead(_ordered(found), ChannelCursor(until), finished=True)


def channel_roles(
    integrations: Mapping[str, Any],
    followups_cfg: Mapping[str, Any],
    *,
    today: date,
) -> list[ChannelRole]:
    channels = _configured_channels(integrations)
    roles = [
        ChannelRole(channels[area], source, place)
        for area, source, place in _PLACES
        if area in channels
    ]
    until = followups_cfg.get("old_channels_until")
    old = followups_cfg.get("old_channels")
    if isinstance(until, date) and today <= until and isinstance(old, list):
        roles.extend(
            ChannelRole(channel.strip(), "slack_old", "private")
            for channel in old
            if isinstance(channel, str) and channel.strip()
        )
    return roles


async def _messages(
    budget: SlackBudget, method: Callable[..., Awaitable[Any]], *, replies: bool, **params: Any
) -> AsyncIterator[dict[str, Any]]:
    cursor = ""
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = await budget.call(
            method,
            replies=replies,
            limit=_PAGE_SIZE,
            include_all_metadata=True,
            **params,
            **extra,
        )
        for message in response.get("messages") or []:
            yield message
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return


def _may_have_new_replies(message: dict[str, Any], cutoff: Decimal) -> bool:
    if (message.get("reply_count") or 0) <= 0:
        return False
    latest = message.get("latest_reply")
    return not latest or Decimal(latest) > cutoff


def _keep(
    found: dict[str, Entry], message: dict[str, Any], reading: _Reading, thread: str | None
) -> None:
    ts = message["ts"]
    if not reading.window[0] < Decimal(ts) <= reading.window[1]:
        return
    bot_id, user, subtype = message.get("bot_id"), message.get("user"), message.get("subtype")
    if bot_id:
        if subtype not in _BOT_SUBTYPES or _is_morning_message(message):
            return
        who = reading.speakers.bot(bot_id)
    elif user and subtype in _PEOPLE_SUBTYPES:
        who = reading.speakers.person(user)
    else:
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    found[ts] = Entry(
        source=reading.role.source,
        ref=reading.permalink(reading.role.channel_id, ts),
        who=who,
        when=_utc(ts),
        text=text,
        place=reading.role.place,
        thread=thread,
    )


def _thread_text(message: dict[str, Any], parent_text: object) -> str | None:
    if message.get("thread_ts") in (None, message["ts"]) or not isinstance(parent_text, str):
        return None
    return parent_text.strip() or None


def _is_morning_message(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata")
    return isinstance(metadata, dict) and metadata.get("event_type") == MORNING_MESSAGE_EVENT


def _ordered(found: dict[str, Entry]) -> list[Entry]:
    return [found[ts] for ts in sorted(found, key=Decimal)]


def _rate_limited(exc: SlackApiError) -> bool:
    response = exc.response
    if getattr(response, "status_code", None) == 429:
        return True
    error = response.get("error") if hasattr(response, "get") else None
    return error == "ratelimited"


def _utc(ts: str) -> str:
    moment = datetime.fromtimestamp(float(ts), UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def speakers_from(
    integrations: Mapping[str, Any], *, owner: str | None, bot_id: str | None
) -> Speakers:
    senders = _notifier_options(integrations).get("authorized_senders")
    co_owners = (
        frozenset(user for user in senders if isinstance(user, str) and user != owner)
        if isinstance(senders, Mapping)
        else frozenset()
    )
    return Speakers(owner=owner or None, co_owners=co_owners, bot_id=bot_id or None)


def _notifier_options(integrations: Mapping[str, Any]) -> Mapping[str, Any]:
    notifier = integrations.get("notifier")
    options = notifier.get("options") if isinstance(notifier, Mapping) else None
    return options if isinstance(options, Mapping) else {}


def _configured_channels(integrations: Mapping[str, Any]) -> dict[str, str]:
    channels = _notifier_options(integrations).get("channels")
    if not isinstance(channels, Mapping):
        return {}
    found: dict[str, str] = {}
    for area, entry in channels.items():
        channel_id = entry.get("channel_id") if isinstance(entry, Mapping) else None
        if isinstance(area, str) and isinstance(channel_id, str) and channel_id.strip():
            found[area] = channel_id.strip()
    return found
