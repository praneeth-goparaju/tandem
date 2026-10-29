from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from pathlib import Path
from typing import Any

from src.brain.atomic_io import (
    StaleWriteError,
    atomic_write_bytes,
    atomic_write_text_if_sha256,
)

__all__ = [
    "extract_body",
    "frontmatter_field",
    "mark_sent_and_copy",
    "read_draft_file",
    "snapshot_draft",
]

_FRONTMATTER_STATUS = re.compile(r"^status:\s*\S+\s*$", re.MULTILINE)
_DRAFT_MAX_BYTES = 1_000_000
_DRAFT_PREFIX = ("00 - Inbox", "drafts")


def frontmatter_field(text: str, key: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    m = pattern.search(block)
    return m.group(1).strip() if m else None


def extract_body(text: str) -> str:
    if not text.startswith("---"):
        return text.strip()
    end = text.find("\n---", 3)
    if end == -1:
        return text.strip()
    return text[end + 4 :].lstrip("\n").rstrip() + "\n"


def _relative_draft_path(value: object) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("draft_path is required")
    relative = Path(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("draft_path must be a safe vault-relative path")
    if relative.parts[:2] != _DRAFT_PREFIX or len(relative.parts) != 3:
        raise ValueError("draft_path must name one file in 00 - Inbox/drafts")
    if relative.suffix.lower() != ".md":
        raise ValueError("draft_path must name a markdown file")
    return relative


def read_draft_file(
    vault_path: Path,
    value: object,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> tuple[Path, bytes, str]:
    relative = _relative_draft_path(value)
    root = vault_path.expanduser().resolve(strict=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(root, os.O_RDONLY | directory | nofollow)
    parent_fd = root_fd
    fd: int | None = None
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow,
                dir_fd=parent_fd,
            )
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        fd = os.open(relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=parent_fd)
        before = os.fstat(fd)
        current = os.stat(relative.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError("draft file changed during validation")
        if hasattr(os, "getuid") and before.st_uid != os.getuid():
            raise PermissionError("draft file must be owned by this user")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(_DRAFT_MAX_BYTES + 1)
        if len(data) > _DRAFT_MAX_BYTES:
            raise ValueError(f"draft file is too large (> {_DRAFT_MAX_BYTES} bytes)")
        after = os.fstat(fd)
        if (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("draft file changed while it was read")
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)

    digest = hashlib.sha256(data).hexdigest()
    if expected_size is not None and len(data) != int(expected_size):
        raise ValueError("draft file size changed after approval")
    if expected_sha256 is not None and not hmac.compare_digest(
        digest, str(expected_sha256)
    ):
        raise ValueError("draft file content changed after approval")
    return root / relative, data, digest


def snapshot_draft(vault_path: Path, value: object) -> tuple[str, dict[str, Any]]:
    path, data, digest = read_draft_file(vault_path, value)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("draft file must be valid UTF-8") from exc
    status = frontmatter_field(text, "status")
    if status == "sent":
        raise ValueError("draft is already marked sent")
    kind = frontmatter_field(text, "kind") or "doc"
    title = frontmatter_field(text, "title") or ""
    recipient = frontmatter_field(text, "recipient") or ""
    preview = ""
    for line in extract_body(text).splitlines():
        cleaned = " ".join(line.lstrip("#> -*").split())
        if cleaned:
            preview = cleaned[:180]
            break
    relative = path.relative_to(vault_path.expanduser().resolve(strict=True)).as_posix()
    return relative, {
        "draft_sha256": digest,
        "draft_size": len(data),
        "draft_kind": kind[:40],
        "draft_recipient": recipient[:180],
        "draft_title": title[:180],
        "draft_preview": preview,
    }


def mark_sent_and_copy(
    src_path: Path,
    vault_path: Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> str:
    relative = src_path.relative_to(vault_path.expanduser().resolve(strict=True))
    src_path, data, digest = read_draft_file(
        vault_path,
        relative,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )
    text = data.decode("utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = text[: end + 1]
            tail = text[end + 1 :]
            new_head = _FRONTMATTER_STATUS.sub("status: sent", head, count=1)
            if new_head == head:
                new_head = head.rstrip("\n") + "\nstatus: sent\n"
            if expected_sha256 and not frontmatter_field(text, "approved_sha256"):
                new_head = (
                    new_head.rstrip("\n")
                    + f"\napproved_sha256: {expected_sha256}\n"
                )
            text = new_head + tail
    try:
        atomic_write_text_if_sha256(src_path, text, digest)
    except StaleWriteError as exc:
        raise ValueError("draft changed before bookkeeping") from exc

    sent_dir = vault_path / "00 - Inbox" / "sent"
    dst = sent_dir / src_path.name
    atomic_write_bytes(dst, text.encode("utf-8"))
    return str(dst.relative_to(vault_path))
