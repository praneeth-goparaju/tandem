"""Turn a vault-relative path from a chat into a safe path on disk.

Every read and write tool goes through ``resolve_in_vault``, and every write
tool also goes through ``check_writable``. Names are normalised to NFC because
macOS may store a name in NFD while a chat sends NFC. APFS looks names up
regardless of normalisation, so the NFC path still opens the file stored in
NFD. Symlinks are followed, and anything that ends up outside the vault folder
is refused.

Protected and owner-only paths can be read but never written. They are
compared case-folded, so ``claude.md`` is refused whether or not the volume
tells upper and lower case apart. Beyond the fixed list, a write is refused
when any part of the path is one of ``INSTRUCTION_NAMES`` (files and folders
that Claude Code, Codex or Obsidian load and follow, wherever they are in the
vault) or starts with a dot, as the daemon's direct-edit rule does.
"""

from __future__ import annotations

import posixpath
import unicodedata
from pathlib import Path, PurePath, PurePosixPath

from src.brain.connector.layout import DEFAULT_LAYOUT, owner_only_paths

PROTECTED: tuple[str, ...] = (
    "CLAUDE.md",
    "Instructions/",
    "Brain Agents/",
    ".claude/",
    ".obsidian/",
    ".trash/",
)
OWNER_ONLY: tuple[str, ...] = owner_only_paths(DEFAULT_LAYOUT)
INSTRUCTION_NAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    ".mcp.json",
    ".claude",
    ".codex",
    ".obsidian",
    ".trash",
    "Instructions",
    "Brain Agents",
)
_INSTRUCTION_KEYS = frozenset(name.casefold() for name in INSTRUCTION_NAMES)


class PathRefused(Exception):
    """The path is outside the vault, or the rules do not allow writing it."""


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _vault_root(vault: Path) -> Path:
    return vault.expanduser().resolve(strict=False)


def _vault_relative(path: Path, root: Path) -> PurePosixPath | None:
    try:
        relative = PurePath(_nfc(str(path))).relative_to(PurePath(_nfc(str(root))))
    except ValueError:
        return None
    return PurePosixPath(relative.as_posix())


def resolve_in_vault(vault: Path, rel: str) -> Path:
    """Return the real path of ``rel`` inside ``vault``.

    The file does not have to exist. Raises ``PathRefused`` when the path is
    absolute or, once symlinks are followed, lies outside the vault.
    """
    wanted = PurePosixPath(_nfc(rel))
    if wanted.is_absolute():
        raise PathRefused(f"{rel} is outside the vault")
    root = _vault_root(vault)
    resolved = (root / wanted).resolve(strict=False)
    if _vault_relative(resolved, root) is None:
        raise PathRefused(f"{rel} is outside the vault")
    return resolved


def _rule_broken(relative: str, owner_only: tuple[str, ...]) -> str | None:
    key = relative.casefold()
    for entry in PROTECTED:
        folded = entry.casefold()
        if folded.endswith("/"):
            if key == folded.rstrip("/") or key.startswith(folded):
                return f"{entry} is protected"
        elif key == folded:
            return f"{entry} is protected"
    for entry in owner_only:
        if key == entry.casefold():
            return f"{PurePosixPath(entry).name} is owner-only"
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if any(part.casefold() in _INSTRUCTION_KEYS for part in parts):
        return f"{relative} is refused: instruction files are protected"
    if any(part.startswith(".") for part in parts):
        return f"{relative} is refused: hidden files and folders are protected"
    return None


def check_writable(vault: Path, rel: str, *, owner_only: tuple[str, ...] = OWNER_ONLY) -> Path:
    """Return the real path of ``rel`` if a chat may write it.

    Refuses everything ``resolve_in_vault`` refuses, plus protected and
    owner-only paths, instruction files and folders anywhere in the vault, and
    hidden files and folders. Both the path as asked for and the path a symlink
    leads to are checked.
    """
    resolved = resolve_in_vault(vault, rel)
    asked = posixpath.normpath(_nfc(rel))
    real = _vault_relative(resolved, _vault_root(vault))
    for relative in (asked, str(real)):
        broken = _rule_broken(relative, owner_only)
        if broken is not None:
            raise PathRefused(broken)
    return resolved
