"""Fill in the launchd templates in ``deploy/launchd/`` and write them to the private overlay."""

from __future__ import annotations

import os
import re
from pathlib import Path
from xml.sax.saxutils import escape

from src.brain.atomic_io import atomic_write_text

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "deploy" / "launchd"
TEMPLATE_SUFFIX = ".plist.example"
TEMPLATE_PREFIX = "com.example"
VAULT_PLACEHOLDER = "__VAULT_PATH__"
OUT_DIR_NAME = "launchd"

_PRIVATE_DIR_MODE = 0o700
_FILE_MODE = 0o600
_LABEL_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_PLACEHOLDER = re.compile(r"__[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*__")
_LEFTOVER = re.compile(r"__[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*__|__")
_TOKEN = re.compile(r"com\.example|__[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*__")
_TEMPLATE_HEADER = re.compile(r"<!--.*?-->\n", re.DOTALL)
_PLIST_OPEN = '<plist version="1.0">\n'


def render_launchd(
    repo_root: Path,
    home: Path,
    user_dir: Path,
    out_dir: Path | None = None,
    label_prefix: str = TEMPLATE_PREFIX,
    vault_path: Path | None = None,
    *,
    timezone: str,
) -> list[Path]:
    """Write one launchd file per template and return the paths written."""
    if not _LABEL_PREFIX.fullmatch(label_prefix):
        raise ValueError(f"label prefix {label_prefix!r} must use letters, digits, dots and dashes")
    values = {
        "__REPO_ROOT__": str(repo_root),
        "__HOME__": str(home),
        "__USER_DIR__": str(user_dir),
        "__TIMEZONE__": timezone,
    }
    if vault_path is not None:
        values[VAULT_PLACEHOLDER] = str(vault_path)

    rendered: list[tuple[str, str]] = []
    for template in sorted(TEMPLATE_DIR.glob(f"*{TEMPLATE_SUFFIX}")):
        text = template.read_text(encoding="utf-8")
        if VAULT_PLACEHOLDER in text and vault_path is None:
            continue
        filled = _fill(template.name, text, values, label_prefix)
        rendered.append((_output_name(template.name, label_prefix), filled))

    folder = _output_folder(Path(out_dir) if out_dir is not None else Path(user_dir) / OUT_DIR_NAME)
    written: list[Path] = []
    for name, text in rendered:
        target = folder / name
        atomic_write_text(target, text, mode=_FILE_MODE, preserve_existing_mode=False)
        written.append(target)
    return written


def _output_name(template_name: str, label_prefix: str) -> str:
    name = template_name.removesuffix(".example")
    return label_prefix + name.removeprefix(TEMPLATE_PREFIX)


def _fill(name: str, text: str, values: dict[str, str], label_prefix: str) -> str:
    text = _TEMPLATE_HEADER.sub("", text, count=1)
    known_removed = _PLACEHOLDER.sub(lambda m: "" if m.group(0) in values else m.group(0), text)
    leftover = _LEFTOVER.search(known_removed)
    if leftover:
        raise ValueError(f"{name}: placeholder {leftover.group(0)} has no value")
    replacements = {TEMPLATE_PREFIX: label_prefix, **values}
    text = _TOKEN.sub(lambda match: escape(replacements[match.group(0)]), text)
    note = f"<!-- Rendered by `python -m src.cli render-launchd` from deploy/launchd/{name}. -->\n"
    return text.replace(_PLIST_OPEN, _PLIST_OPEN + note, 1)


def _output_folder(folder: Path) -> Path:
    folder = folder.expanduser()
    if not folder.exists():
        folder.mkdir(parents=True)
        os.chmod(folder, _PRIVATE_DIR_MODE)
    return folder
