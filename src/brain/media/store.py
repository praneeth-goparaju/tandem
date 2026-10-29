from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Sequence
from pathlib import Path

from src.brain.atomic_io import atomic_write_bytes, ensure_real_directory
from src.db import expire_media, get_media, mark_media_sent, record_event, record_media
from src.orchestrator.config import REPO_ROOT

log = logging.getLogger(__name__)

MAX_MEDIA_IDS = 4
MEDIA_ID_PATTERN = r"m-[0-9a-f]{12}"
_MEDIA_ID = re.compile(MEDIA_ID_PATTERN)
_STORED_PATH = re.compile(rf"media/({MEDIA_ID_PATTERN})\.png")


def media_dir(user_dir: Path | None = None) -> Path:
    if user_dir is None:
        user_dir = Path(os.environ.get("USER_DIR") or REPO_ROOT / "user")
    return ensure_real_directory(user_dir / "media")


def new_media_id() -> str:
    return f"m-{secrets.token_hex(6)}"


def save_png(
    png: bytes, *, kind: str, agent: str, run_label: str, area: str | None, place: str,
) -> str:
    media_id = new_media_id()
    target = media_dir() / f"{media_id}.png"
    atomic_write_bytes(target, png, mode=0o600)
    try:
        record_media(
            media_id=media_id, agent=agent, run_label=run_label, area=area, place=place,
            kind=kind, path=f"media/{target.name}", size=len(png),
            sha256=hashlib.sha256(png).hexdigest(),
        )
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    record_event(
        actor=f"media:{agent}", action="media_rendered", subject=media_id,
        detail=json.dumps({"kind": kind, "bytes": len(png), "place": place}),
    )
    return media_id


def _live_row(media_id: str) -> dict | None:
    if not _MEDIA_ID.fullmatch(media_id):
        return None
    row = get_media(media_id)
    return None if row is None or row["deleted_at"] else row


def _file_of(media_id: str) -> Path | None:
    path = media_dir() / f"{media_id}.png"
    return path if path.is_file() else None


def parse_media_ids(value: object) -> list[str]:
    """The ids in a `media_ids` argument, in order and without repeats.

    Raises ValueError, in a sentence a model can act on, for anything but a
    list of at most `MAX_MEDIA_IDS` strings that are media ids.
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("media_ids must be a list of media id strings")
    if len(value) > MAX_MEDIA_IDS:
        raise ValueError(f"at most {MAX_MEDIA_IDS} media ids")
    if not all(_MEDIA_ID.fullmatch(item) for item in value):
        raise ValueError("media_ids must be ids like m-0123456789ab")
    return list(dict.fromkeys(value))


def media_for_place(
    media_ids: Sequence[str], place: str | None,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Split `media_ids` into what may be posted in `place`, with the files, and what may not.

    An id may be posted when its row is live, was made for `place` and its file
    is still there. Order is kept and a repeated id counts once. With no
    `place`, nothing may be posted.
    """
    accepted: list[tuple[str, Path]] = []
    refused: list[str] = []
    for media_id in dict.fromkeys(media_ids):
        row = _live_row(media_id)
        path = _file_of(media_id) if row is not None else None
        if place is not None and row is not None and row["place"] == place and path is not None:
            accepted.append((media_id, path))
        else:
            refused.append(media_id)
    return accepted, refused


def mark_sent(media_id: str, *, agent: str, channel: str, ts: str) -> None:
    mark_media_sent(media_id, channel=channel, ts=ts)
    row = get_media(media_id) or {}
    record_event(
        actor=f"media:{agent}", action="media_sent", subject=media_id,
        detail=json.dumps({"channel": channel, "place": row.get("place"), "bytes": row.get("bytes")}),
    )


def delete_expired(older_than_days: int = 30) -> int:
    folder = media_dir()
    stored = expire_media(older_than_days)
    for entry in stored:
        match = _STORED_PATH.fullmatch(entry)
        if match is None:
            log.warning("media row path %r is not a media file; left alone", entry)
            continue
        try:
            (folder / f"{match[1]}.png").unlink(missing_ok=True)
        except OSError as exc:
            log.warning("media file %s was not deleted: %s", match[1], exc)
    return len(stored)
