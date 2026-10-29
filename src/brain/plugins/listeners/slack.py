from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from collections.abc import Awaitable, Callable
from functools import partial

import aiohttp

from src.brain.followups.cards import CARD_ACTIONS
from src.brain.followups.views import ACTION_IDS as DIGEST_ACTION_IDS
from src.brain.followups.views import home_attempts
from src.brain.media.voice import ClipOverLimit
from src.brain.net_security import read_bounded, safe_https_url
from src.brain.plugins.notifiers.slack import (
    APPROVE_ACTION_ID,
    FINDING_EVENT_TYPE,
    REJECT_ACTION_ID,
)

log = logging.getLogger("brain.plugins.listener.slack")

_REF_RE = re.compile(r"ref:([A-Za-z0-9_-]+)")

_HEARTBEAT_INTERVAL_SEC = 60.0
_MAX_CHAT_IMAGES = 4
_MAX_CHAT_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_CHAT_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024
_SUPPORTED_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})
_SLACK_FILE_HOSTS = frozenset({"files.slack.com"})
_MAX_CHAT_VOICE_BYTES = 25 * 1024 * 1024
_MAX_CHAT_VOICE_MS = 10 * 60 * 1000
_VOICE_DOWNLOAD_TIMEOUT_SEC = 60
_VOICE_SUBTYPES = frozenset({"slack_audio", "slack_video"})
_AUDIO_MIME_PREFIX = "audio/"


def _chat_image_candidates(files: list[dict]) -> tuple[list[tuple[str, str]], int]:
    candidates: list[tuple[str, str]] = []
    image_count = 0
    declared_total = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mimetype") or "").lower()
        if not mime.startswith("image/"):
            continue
        image_count += 1
        url = str(item.get("url_private_download") or item.get("url_private") or "")
        safe_url = safe_https_url(url, allowed_hosts=_SLACK_FILE_HOSTS)
        size = item.get("size")
        if (
            len(candidates) >= _MAX_CHAT_IMAGES
            or mime not in _SUPPORTED_IMAGE_TYPES
            or (isinstance(size, int) and not 0 <= size <= _MAX_CHAT_IMAGE_BYTES)
            or (isinstance(size, int) and declared_total + size > _MAX_CHAT_IMAGE_TOTAL_BYTES)
            or safe_url is None
        ):
            continue
        candidates.append((safe_url, mime))
        declared_total += size if isinstance(size, int) else 0
    return candidates, image_count


async def _download_chat_images(
    candidates: list[tuple[str, str]], web_client
) -> list[dict]:
    images: list[dict] = []
    token = getattr(web_client, "token", None)
    if not token:
        return images

    headers = {"Authorization": f"Bearer {token}"}
    total_bytes = 0
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(
        headers=headers, timeout=timeout, trust_env=False
    ) as session:
        for url, mime in candidates:
            try:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Slack file returned HTTP {response.status}")
                    data = await read_bounded(response.content, _MAX_CHAT_IMAGE_BYTES)
                if (
                    len(data) > _MAX_CHAT_IMAGE_BYTES
                    or total_bytes + len(data) > _MAX_CHAT_IMAGE_TOTAL_BYTES
                ):
                    continue
                images.append({
                    "type": "image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime,
                })
                total_bytes += len(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("Slack: image download failed (%s)", type(exc).__name__)
    return images


def _clip_type(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    mime = str(item.get("mimetype") or "").lower()
    if item.get("subtype") in _VOICE_SUBTYPES or mime.startswith(_AUDIO_MIME_PREFIX):
        return mime
    return None


def _clip_url(item: dict) -> str | None:
    url = str(item.get("url_private_download") or item.get("url_private") or "")
    return safe_https_url(url, allowed_hosts=_SLACK_FILE_HOSTS)


def _declares_too_much(item: dict) -> bool:
    size, duration = item.get("size"), item.get("duration_ms")
    return (isinstance(size, int) and size > _MAX_CHAT_VOICE_BYTES) or (
        isinstance(duration, (int, float)) and duration > _MAX_CHAT_VOICE_MS
    )


def _usable_clips(files: list[dict]) -> list[tuple[dict, str, str]]:
    clips: list[tuple[dict, str, str]] = []
    for item in files:
        mime = _clip_type(item)
        if mime is None:
            continue
        url = _clip_url(item)
        size = item.get("size")
        if url is None or _declares_too_much(item) or (isinstance(size, int) and size < 0):
            continue
        clips.append((item, url, mime))
    return clips


def _chat_voice_over_limit(files: list[dict]) -> bool:
    return any(
        _clip_type(item) is not None and _clip_url(item) is not None and _declares_too_much(item)
        for item in files
    )


async def _download_chat_voice(
    candidates: list[tuple[str, str]], web_client
) -> bytes | None:
    token = getattr(web_client, "token", None)
    if not token or not candidates:
        return None

    url, _mime = candidates[0]
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=_VOICE_DOWNLOAD_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(
            headers=headers, timeout=timeout, trust_env=False
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError(f"Slack file returned HTTP {response.status}")
                data = await read_bounded(response.content, _MAX_CHAT_VOICE_BYTES)
    except Exception as exc:
        log.warning("Slack: voice download failed (%s)", type(exc).__name__)
        return None
    if len(data) > _MAX_CHAT_VOICE_BYTES:
        raise ClipOverLimit("the clip is larger than 25 MiB")
    return data


async def _refuse_chat_voice() -> bytes:
    raise ClipOverLimit("the clip declares more than 25 MiB or 10 minutes")


def _may_send_voice(sender: str) -> bool:
    try:
        from src.orchestrator.reply_routing import sender_identity
        from src.orchestrator.runners.run_guard import feature_on

        return feature_on("BRAIN_MEDIA", os.environ) and sender_identity(sender) is not None
    except Exception as exc:
        log.warning("Slack: sender check failed (%s)", type(exc).__name__)
        return False


def _voice_loader(
    files: list[dict], sender: str, web_client
) -> tuple[Callable[[], Awaitable[bytes | None]] | None, bool]:
    own = [item for item in files if isinstance(item, dict) and sender and item.get("user") == sender]
    loader: Callable[[], Awaitable[bytes | None]]
    clips = _usable_clips(own)
    attached = False
    if clips:
        loader = partial(_download_chat_voice, [(url, mime) for _item, url, mime in clips], web_client)
        attached = clips[0][0].get("subtype") not in _VOICE_SUBTYPES
    elif _chat_voice_over_limit(own):
        loader = _refuse_chat_voice
    else:
        return None, False
    return (loader, attached) if _may_send_voice(sender) else (None, False)


def _stamp_heartbeat() -> None:
    try:
        from src.brain.state import set_state
        from src.db import utcnow

        set_state("slack_listener_heartbeat", utcnow())
    except Exception as e:  # noqa: BLE001
        log.debug(f"slack listener heartbeat write failed: {e}")


class SlackListener:
    def __init__(
        self,
        bot_token: str | None = None,
        app_token: str | None = None,
        **_ignored,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("SLACK_BOT_TOKEN", "")
        self.app_token = app_token or os.environ.get("SLACK_APP_TOKEN", "")

    async def run(self, callback) -> None:
        if not (self.bot_token and self.app_token):
            log.warning(
                "SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set — Slack listener disabled"
            )
            return

        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            log.error("slack_sdk not installed. Run: pip install slack_sdk>=3.27")
            return

        web_client = AsyncWebClient(token=self.bot_token)
        web_client.proxy = None
        sm = SocketModeClient(
            app_token=self.app_token,
            web_client=web_client,
        )
        sm.proxy = None

        async def _on_request(client, request) -> None:
            # Always ack first; Slack times out unacked events.
            from slack_sdk.socket_mode.response import SocketModeResponse

            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=request.envelope_id)
            )

            if request.type == "interactive":
                envelope = _normalize_interactive(request.payload or {})
                if envelope is None:
                    return
                try:
                    result = await callback(envelope)
                    if result and envelope["type"] in ("digest", "card"):
                        await _respond_to_digest_result(
                            web_client, {**result, "user": envelope.get("sender", "")}
                        )
                    elif result:
                        await _respond_to_approval_result(
                            web_client, request.payload or {}, result
                        )
                except Exception as e:  # noqa: BLE001
                    log.error(f"Slack interactive callback error: {e}", exc_info=True)
                return

            if request.type != "events_api":
                return

            event = (request.payload or {}).get("event") or {}
            etype = str(event.get("type") or "")
            try:
                envelope = await _normalize_event(event, etype, web_client)
            except Exception as e:  # noqa: BLE001
                log.warning(f"Slack: failed to normalize event {etype}: {e}")
                return

            if envelope is None:
                return

            try:
                result = await callback(envelope)
                if result and envelope.get("type") == "home_opened":
                    await _respond_to_digest_result(web_client, result)
            except Exception as e:  # noqa: BLE001
                log.error(f"Slack callback error: {e}", exc_info=True)

        sm.socket_mode_request_listeners.append(_on_request)

        backoff = 5.0
        try:
            while True:
                try:
                    log.info("Slack Socket Mode connecting...")
                    await sm.connect()
                    log.info("Slack Socket Mode connected.")
                    backoff = 5.0
                    while True:
                        _stamp_heartbeat()
                        await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        f"Slack Socket Mode error: {e}; reconnecting in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300)
        finally:
            try:
                await sm.close()
            except Exception as e:  # noqa: BLE001
                log.warning(f"Slack Socket Mode close failed: {e}")


def _normalize_digest_click(payload: dict, action: dict) -> dict:
    selected = action.get("selected_options")
    if isinstance(selected, list):
        value = str((selected[0] or {}).get("value") or "") if selected else ""
    else:
        value = str(action.get("value") or "")
    return {
        "type": "digest",
        "action": action.get("action_id"),
        "value": value,
        "sender": (payload.get("user") or {}).get("id", ""),
        "group_id": (payload.get("channel") or {}).get("id", ""),
        "target_timestamp": str((payload.get("message") or {}).get("ts") or ""),
        "trigger_id": str(payload.get("trigger_id") or ""),
        "container": str((payload.get("container") or {}).get("type") or ""),
        "timestamp": str(payload.get("action_ts") or action.get("action_ts") or ""),
    }


def _normalize_interactive(payload: dict) -> dict | None:
    if payload.get("type") != "block_actions":
        return None
    actions = payload.get("actions") or []
    if len(actions) != 1:
        return None
    action = actions[0]
    action_name = action.get("action_id")
    if action_name in DIGEST_ACTION_IDS:
        return _normalize_digest_click(payload, action)
    if action_name in CARD_ACTIONS:
        return {**_normalize_digest_click(payload, action), "type": "card"}
    if action_name == APPROVE_ACTION_ID:
        decision = "approved"
    elif action_name == REJECT_ACTION_ID:
        decision = "rejected"
    else:
        return None
    action_id = str(action.get("value") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{12}", action_id):
        return None
    channel = payload.get("channel") or {}
    message = payload.get("message") or {}
    return {
        "type": "approval",
        "decision": decision,
        "action_id": action_id,
        "sender": (payload.get("user") or {}).get("id", ""),
        "group_id": channel.get("id", ""),
        "timestamp": str(payload.get("action_ts") or payload.get("trigger_id") or ""),
        "target_timestamp": str(message.get("ts") or ""),
    }


async def _update_approval_message(web_client, payload: dict, result: dict) -> None:
    if result.get("outcome") not in {"approved", "rejected", "already_resolved"}:
        return
    message = payload.get("message") or {}
    channel = (payload.get("channel") or {}).get("id")
    ts = message.get("ts")
    if not (channel and ts):
        return
    action_id = result.get("action_id", "")
    blocks = []
    for block in message.get("blocks") or []:
        if block.get("block_id") == f"brain_approval_{action_id}":
            if result["outcome"] == "approved":
                status = "Approved — queued for execution"
            else:
                status = result["outcome"].replace("_", " ").title()
            blocks.append({
                "type": "context",
                "block_id": f"brain_approval_result_{action_id}",
                "elements": [{"type": "mrkdwn", "text": f"*{status}* · `{action_id}`"}],
            })
        else:
            blocks.append(block)
    await web_client.chat_update(
        channel=channel,
        ts=ts,
        text=message.get("text") or "Approval updated",
        blocks=blocks,
    )


async def _respond_to_digest_result(web_client, result: dict) -> None:
    for update in [result["update"]] if result.get("update") else result.get("updates") or []:
        await web_client.chat_update(
            channel=update["channel"], ts=update["ts"], text=update["text"], blocks=update["blocks"],
        )
    if result.get("modal") and result.get("trigger_id"):
        await web_client.views_open(trigger_id=result["trigger_id"], view=result["modal"])
    notice = result.get("ephemeral")
    if notice and notice.get("channel") and result.get("user"):
        await web_client.chat_postEphemeral(
            channel=notice["channel"], user=result["user"], text=notice["text"],
        )
    home = result.get("home")
    if home:
        for attempt in home_attempts(home["view"]):
            try:
                response = await web_client.views_publish(user_id=home["user"], view=attempt)
            except Exception as exc:  # noqa: BLE001
                log.warning("home tab not published: %s", exc)
                continue
            if not (hasattr(response, "get") and response.get("ok") is False):
                break


async def _respond_to_approval_result(
    web_client,
    payload: dict,
    result: dict,
) -> None:
    outcome = str(result.get("outcome") or "")
    if outcome in {"approved", "rejected", "already_resolved"}:
        await _update_approval_message(web_client, payload, result)
        return

    messages = {
        "unauthorized": (
            "Approval was not applied: this channel is not mapped for the "
            "action, or you are not authorized. No action ran."
        ),
        "not_found": "Approval was not applied: this action no longer exists. No action ran.",
        "invalid": "Approval was not applied: this approval control is invalid. No action ran.",
    }
    text = messages.get(outcome)
    channel = (payload.get("channel") or {}).get("id")
    user = (payload.get("user") or {}).get("id")
    if not (text and channel and user):
        return
    kwargs = {"channel": channel, "user": user, "text": text}
    message_ts = (payload.get("message") or {}).get("ts")
    if message_ts:
        kwargs["thread_ts"] = message_ts
    await web_client.chat_postEphemeral(**kwargs)


async def _normalize_event(event: dict, etype: str, web_client) -> dict | None:
    if etype == "reaction_added" or etype == "reaction_removed":
        item = event.get("item") or {}
        return {
            "type": "reaction",
            "emoji": event.get("reaction", ""),
            "sender": event.get("user", ""),
            "group_id": item.get("channel", ""),
            "timestamp": str(event.get("event_ts", "")),
            "target_timestamp": str(item.get("ts", "")),
            "quoted_timestamp": None,
            "text": "",
            "quoted_text": "",
            "finding_id": None,
            "removed": etype == "reaction_removed",
        }

    if etype == "app_home_opened":
        if event.get("tab") != "home":
            return None
        return {"type": "home_opened", "sender": event.get("user", ""), "timestamp": ""}

    if etype != "message":
        return None

    subtype = event.get("subtype")
    if subtype in {"message_changed", "message_deleted", "bot_message", "channel_join"}:
        return None
    if event.get("bot_id"):
        return None

    text = event.get("text", "") or ""
    channel = event.get("channel", "")
    channel_type = event.get("channel_type", "")
    ts = str(event.get("ts", ""))
    thread_ts = event.get("thread_ts")
    files = list(event.get("files") or [])
    image_candidates, image_count = _chat_image_candidates(files)
    sender = event.get("user", "")
    voice_loader, voice_attached = _voice_loader(files, sender, web_client)

    finding_id = None
    parent_agent = None
    parent_area = None
    quoted_text = ""
    quoted_from_bot = False

    if thread_ts and thread_ts != ts:
        try:
            resp = await web_client.conversations_replies(
                channel=channel, ts=thread_ts, limit=1, include_all_metadata=True
            )
            msgs = (resp.get("messages") or []) if resp.get("ok") else []
            if msgs:
                parent = msgs[0]
                quoted_text = parent.get("text", "") or ""
                quoted_from_bot = (
                    bool(parent.get("bot_id")) or parent.get("subtype") == "bot_message"
                )
                meta = parent.get("metadata") or {}
                if meta.get("event_type") == FINDING_EVENT_TYPE:
                    payload = meta.get("event_payload") or {}
                    fid = payload.get("finding_id")
                    if fid:
                        finding_id = fid
                    parent_agent = payload.get("agent") or None
                    parent_area = payload.get("area") or None
        except Exception as e:  # noqa: BLE001
            log.debug(f"Slack: quoted-text fetch failed for {channel}/{thread_ts}: {e}")

    if not finding_id:
        m = _REF_RE.search(quoted_text) if quoted_text else None
        if m:
            finding_id = m.group(1)
            log.info("slack listener: resolved finding_id via legacy ref: regex")

    return {
        "type": "reply",
        "text": text,
        "emoji": None,
        "sender": sender,
        "group_id": channel,
        "channel_type": channel_type,
        "timestamp": ts,
        "quoted_timestamp": str(thread_ts) if thread_ts else None,
        "target_timestamp": None,
        "quoted_text": quoted_text,
        "quoted_from_bot": quoted_from_bot,
        "finding_id": finding_id,
        "parent_agent": parent_agent,
        "parent_area": parent_area,
        "images": [],
        "image_count": image_count,
        "_load_images": partial(
            _download_chat_images, image_candidates, web_client
        ) if image_candidates else None,
        "_load_voice": voice_loader,
        "voice_attached": voice_attached,
    }
