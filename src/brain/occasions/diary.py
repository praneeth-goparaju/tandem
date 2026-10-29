from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from src.brain.atomic_io import atomic_write_text, confined_path, read_regular_text
from src.brain.occasions import store
from src.orchestrator import config

SECTIONS = [
    "Profile",
    "Timeline",
    "Open threads",
    "Resolved threads",
    "How I feel",
    "Inside jokes",
    "Shared memories",
]

_CHECKED_THREAD_RE = re.compile(r"(?:^|\s—\s)\[[xX]\]\s+")


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _template(name: str) -> str:
    return f"# {name}\n\n" + "".join(f"## {s}\n\n" for s in SECTIONS)


def _insert_under_section(md: str, section: str, line: str) -> str:
    header = f"## {section}"
    lines = md.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() == header:
            at = i + 1
            if at < len(lines) and lines[at].strip() == "":
                at += 1
            lines.insert(at, line)
            return "\n".join(lines)
    if not md.endswith("\n"):
        md += "\n"
    return f"{md}\n## {section}\n\n{line}\n"


def append_entry(person_id: str, text: str, section: str = "Timeline",
                 *, when: str | None = None) -> str:
    """Append a dated entry under `section` of the person's diary note.

    Creates the note (from the template) if missing. Bumps `last_interaction`
    for Timeline entries. Returns the vault-relative path. Raises ValueError on
    an unknown person or a path that escapes the vault.
    """
    p = store.people_by_id().get(person_id)
    if not p:
        raise ValueError(f"no person '{person_id}' in people.yaml")
    if not (text or "").strip():
        raise ValueError("empty entry text")

    rel = store.dossier_path(p)
    try:
        path = confined_path(config.VAULT_PATH, rel)
    except ValueError as e:
        raise ValueError("dossier path escapes the vault") from e

    try:
        md = read_regular_text(path)
    except FileNotFoundError:
        md = _template(p.name)
    except OSError as exc:
        raise ValueError("dossier is not a safe regular file") from exc
    md = _insert_under_section(md, section, f"- {when or _today()} — {text.strip()}")
    atomic_write_text(path, md)

    if section == "Timeline":
        store.bump_last_interaction(person_id, when)
    return rel


def _section_lines(md: str, section: str) -> list[str]:
    out: list[str] = []
    in_sec = False
    for ln in md.split("\n"):
        if ln.strip().startswith("## "):
            in_sec = ln.strip() == f"## {section}"
            continue
        if in_sec and ln.strip().startswith("- "):
            out.append(ln.strip()[2:].strip())
    return out


def _thread_id(person_id: str, thread: str) -> str:
    material = f"{person_id}\0{thread}".encode()
    return hashlib.sha256(material).hexdigest()[:12]


def _active_threads(md: str) -> list[str]:
    return [
        thread
        for thread in _section_lines(md, "Open threads")
        if not _CHECKED_THREAD_RE.search(thread)
    ]


def list_open_threads() -> list[dict]:
    out: list[dict] = []
    for p in store.load_people().people:
        try:
            path = confined_path(config.VAULT_PATH, store.dossier_path(p))
        except ValueError:
            continue
        try:
            text = read_regular_text(path)
        except OSError:
            continue
        for thread in _active_threads(text):
            out.append({
                "thread_id": _thread_id(p.id, thread),
                "person_id": p.id,
                "person_name": p.name,
                "thread": thread,
            })
    return out


def resolve_thread(
    person_id: str,
    contains: str | None = None,
    outcome: str | None = None,
    *,
    thread_id: str | None = None,
    when: str | None = None,
) -> bool:
    """Resolve one exact thread and retain it in a dated history section.

    ``thread_id`` is the safe public selector. ``contains`` remains as a legacy
    compatibility path, but it is accepted only when it identifies exactly one
    active line. Ambiguous selectors fail without changing the note.
    """
    p = store.people_by_id().get(person_id)
    if not p:
        raise ValueError(f"no person '{person_id}' in people.yaml")
    try:
        path = confined_path(config.VAULT_PATH, store.dossier_path(p))
    except ValueError as exc:
        raise ValueError("dossier path escapes the vault") from exc
    try:
        text = read_regular_text(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("dossier is not a safe regular file") from exc
    if not (thread_id or contains):
        raise ValueError("thread_id is required")

    in_sec = False
    candidates: list[tuple[int, str]] = []
    lines = text.split("\n")
    for index, ln in enumerate(lines):
        if ln.strip().startswith("## "):
            in_sec = ln.strip() == "## Open threads"
            continue
        if not in_sec or not ln.strip().startswith("- "):
            continue
        thread = ln.strip()[2:].strip()
        if _CHECKED_THREAD_RE.search(thread):
            continue
        if thread_id and _thread_id(person_id, thread) == thread_id:
            candidates.append((index, thread))
        elif not thread_id and contains and contains.casefold() in thread.casefold():
            candidates.append((index, thread))

    if not candidates:
        return False
    if len(candidates) != 1:
        raise ValueError(
            "thread selector is ambiguous; use the exact thread_id from "
            "humans_open_threads"
        )

    index, thread = candidates[0]
    del lines[index]
    closed_on = when or _today()
    resolved = f"- {closed_on} — [{_thread_id(person_id, thread)}] {thread}"
    clean_outcome = " ".join((outcome or "").split())
    if clean_outcome:
        resolved += f" — outcome: {clean_outcome}"
    updated = _insert_under_section("\n".join(lines), "Resolved threads", resolved)
    if clean_outcome:
        updated = _insert_under_section(
            updated, "Timeline", f"- {closed_on} — {clean_outcome}"
        )
    atomic_write_text(path, updated)
    if clean_outcome:
        store.bump_last_interaction(person_id, closed_on)
    return True
