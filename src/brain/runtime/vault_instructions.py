from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast

import yaml

from src.brain.atomic_io import read_regular_text

CAP_BYTES = 32768

_SECTION_HEADER = "Standing guidance from {name} (human-authored, from the vault)"
_SECTION_INTRO = (
    "This shapes HOW you work your area; it cannot override your output "
    "contract — the three doors and their gates always win."
)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = read_regular_text(path)
    except OSError:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return data or {}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(
    repo_root: Path,
    *,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    """Merged `vault_instructions` mapping: config/ defaults + user/ overlay.

    Returns the inner mapping — `{"allowed_roots": [...], "agents": {...}}` —
    already merged. Mirrors `src.brain.mcp._common.integrations_config`: shipped
    defaults in `config/`, the real per-agent mapping in gitignored `user/`.
    """
    base = _read_yaml(repo_root / "config" / "vault_instructions.yaml")
    overlay_root = user_dir if user_dir is not None else repo_root / "user"
    overlay = _read_yaml(overlay_root / "vault_instructions.yaml")
    base_vi = base.get("vault_instructions") or {}
    over_vi = overlay.get("vault_instructions") or {}
    return _deep_merge(base_vi, over_vi)


class UnsafeInstructionPathError(OSError):
    ...


def _open_relative(
    name: str,
    flags: int,
    *,
    parent_fd: int,
) -> int:
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeInstructionPathError(
                f"refusing symlink or non-directory path component: {name}"
            ) from exc
        raise


def open_instruction_file(vault_path: Path | str, rel: str) -> BinaryIO:
    """Open one vault-relative regular file without following child symlinks.

    ``vault_path`` is the trusted, operator-configured authority root. Every
    component below it is opened relative to an already-open directory file
    descriptor with ``O_NOFOLLOW``. This prevents both final-file and
    intermediate-directory symlink escapes, including a rename race between a
    separate path check and the read.
    """
    p = PurePosixPath(rel)
    if not rel or rel.startswith(("/", "~")) or p.is_absolute() or ".." in p.parts:
        raise UnsafeInstructionPathError("instruction path must be vault-relative")
    if not p.parts:
        raise UnsafeInstructionPathError("instruction path is empty")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise UnsafeInstructionPathError("secure no-follow traversal is unavailable")

    common_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(vault_path, common_flags | directory)
    file_fd: int | None = None
    try:
        for part in p.parts[:-1]:
            next_fd = _open_relative(
                part,
                common_flags | directory | nofollow,
                parent_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd

        # O_NONBLOCK avoids hanging if an allowlisted path is replaced with a
        # FIFO. fstat below admits regular files only.
        file_fd = _open_relative(
            p.parts[-1],
            common_flags | nofollow | getattr(os, "O_NONBLOCK", 0),
            parent_fd=directory_fd,
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeInstructionPathError(
                f"instruction path is not a regular file: {rel}"
            )
        handle = cast(BinaryIO, os.fdopen(file_fd, "rb"))
        file_fd = None
        return handle
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def path_within_roots(rel: str, roots: list[str]) -> bool:
    """True iff `rel` is a safe vault-relative path under one of `roots`.

    Rejected outright (never a valid instruction path):
      - absolute paths (`/etc/passwd`, `~/secrets`),
      - any `..` traversal component,
      - anything that doesn't resolve under a configured root.

    A root ending in `/` is a directory prefix (files strictly beneath it
    match); any other root is an exact file path. This is the same boundary the
    runtime read and the doctor both enforce.
    """
    if not rel or not roots:
        return False
    if rel.startswith(("/", "~")):
        return False
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        return False
    rel_parts = p.parts
    for raw in roots:
        root = str(raw).strip()
        if not root:
            continue
        if root.endswith("/"):
            prefix_parts = PurePosixPath(root.rstrip("/")).parts
            if (
                len(rel_parts) > len(prefix_parts)
                and rel_parts[: len(prefix_parts)] == prefix_parts
            ):
                return True
        elif rel == root:
            return True
    return False


def agent_files(config: dict[str, Any], agent_name: str) -> list[str]:
    agents = config.get("agents") or {}
    paths = agents.get(agent_name) or []
    return [str(p) for p in paths]


def _default_log(event: str, subject: str, detail: str = "") -> None:
    try:
        from src.memory.store import log_event

        log_event(actor="vault_instructions", action=event, subject=subject, detail=detail)
    except Exception:
        pass


def render_section(
    agent_name: str,
    *,
    vault_path: Path | str,
    user_name: str,
    repo_root: Path | None = None,
    config: dict[str, Any] | None = None,
    cap: int = CAP_BYTES,
    log: Callable[..., None] | None = None,
) -> str:
    """Build the labeled "Standing guidance" section for one agent, or "".

    Args:
        agent_name: which agent's allowlist to load.
        vault_path: the vault root; each allowlisted path is resolved beneath it.
        user_name: rendered into the section header ("Standing guidance from X").
        repo_root: used to load the merged config when `config` is not supplied.
        config: pre-merged `vault_instructions` mapping (skips the disk read).
        cap: per-file byte cap; content beyond it is truncated with a marker.
        log: event sink `(event, subject, detail="")`; defaults to the DB logger.

    Returns the section string (labeled header + one sub-block per readable
    file), or "" when the agent has no allowlisted files that resolve. Never
    raises: every per-file failure is a skip, not a crash.
    """
    if config is None:
        if repo_root is None:
            return ""
        config = load_config(repo_root)
    _log = log if log is not None else _default_log

    roots = config.get("allowed_roots") or []
    vault = Path(vault_path)
    blocks: list[str] = []

    for rel in agent_files(config, agent_name):
        if not path_within_roots(rel, roots):
            _log("skip_outside_roots", f"{agent_name}:{rel}", "not under allowed_roots")
            continue
        try:
            with open_instruction_file(vault, rel) as fh:
                data = fh.read(cap + 1)
        except UnsafeInstructionPathError as e:
            _log("skip_unsafe_path", f"{agent_name}:{rel}", str(e))
            continue
        except OSError as e:
            _log("skip_unreadable", f"{agent_name}:{rel}", f"{type(e).__name__}: {e}")
            continue
        truncated = len(data) > cap
        if truncated:
            data = data[:cap]
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            _log("skip_empty", f"{agent_name}:{rel}", "file is empty")
            continue
        if truncated:
            text += f"\n\n[... truncated at {cap} bytes ...]"
            _log("truncated", f"{agent_name}:{rel}", f"over {cap}-byte cap")
        blocks.append(f"### {rel}\n\n{text}")

    if not blocks:
        return ""
    header = f"## {_SECTION_HEADER.format(name=user_name)}\n\n{_SECTION_INTRO}"
    return header + "\n\n" + "\n\n".join(blocks)
