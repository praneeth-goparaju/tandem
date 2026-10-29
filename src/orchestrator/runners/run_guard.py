from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.brain.runtime.vault_paths import names_path, path_in_vault_paths

BRAIN_TOOL_PREFIX = "mcp__brain-tools__"

_BUILTIN_NAMES = {"Read": "read", "Glob": "find", "Grep": "grep"}

ALLOWED_AFTER_WEB = frozenset({
    "web_read",
    "reply_to_message",
    "propose_action",
    "store_finding",
})

READS_AFTER_WEB = frozenset({
    "read", "find", "grep",
    "calendar_list_events", "calendar_get_event", "reminders_list", "reminders_get", "tasks_list",
    "get_recent_findings", "search_knowledge", "follow_ups_list", "area_notes",
    "garmin_daily", "garmin_trends",
})

MAX_WEB_SEARCHES = 3

UNATTENDED_WRITERS_AFTER_READING = frozenset({"create_draft"})

HUMANS_LOOKUP_MUTATORS = frozenset({
    "humans_add_person", "humans_log", "humans_resolve_thread",
    "humans_set_occasion", "humans_update_person", "occasions_learn_edit",
})

DIRECT_MUTATORS = frozenset({
    "create_draft", "intake_dismiss",
    *HUMANS_LOOKUP_MUTATORS,
    "unwatch_search", "watch_search",
})

UNTRUSTED_OUTPUTS = frozenset({
    "read", "find", "grep",
    "get_recent_findings",
    "search_knowledge", "list_pending_actions",
    "mail_search", "mail_get_message", "mail_list_attachments", "mail_get_attachment",
    "calendar_list_events", "calendar_get_event", "reminders_list", "reminders_get",
    "contacts_search", "contacts_get", "tasks_list",
    "humans_find", "humans_read", "humans_due", "humans_open_threads", "humans_meetings",
    "occasions_scan", "occasions_get_dossier",
    "intake_pending", "public_discovery_candidates",
    "search_houses", "process_watches",
})

_CONFINED = "File tools are confined to the configured read roots"


def bare_tool_name(tool_name: str) -> str:
    if tool_name in _BUILTIN_NAMES:
        return _BUILTIN_NAMES[tool_name]
    if tool_name.startswith(BRAIN_TOOL_PREFIX):
        return tool_name.removeprefix(BRAIN_TOOL_PREFIX)
    return tool_name


def _resolve_through_existing_parent(candidate: Path) -> Path:
    suffix: list[str] = []
    existing = candidate
    while not existing.exists():
        parent = existing.parent
        if parent == existing:
            break
        suffix.insert(0, existing.name)
        existing = parent
    resolved = existing.resolve() if existing.exists() else existing
    return resolved.joinpath(*suffix)


def _inside(root: Path, target: Path) -> bool:
    resolved_root = Path(root).resolve()
    candidate = _resolve_through_existing_parent(target)
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError:
        return False
    return not any(part.startswith(".") for part in relative.parts)


# Claude Code trims a path with JavaScript's `trim`, which also removes a byte-order mark.
_JS_WHITESPACE = "".join(map(chr, (
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
)))


def _tool_target(value: str, cwd: Path) -> Path | None:
    """Claude Code trims a path, reads an empty one as `cwd` and joins a relative one to it."""
    text = value.strip(_JS_WHITESPACE)
    if text.startswith(("~", "@")):
        return None
    return Path(os.path.normpath(Path(cwd) / text))


def _unsafe_pattern(pattern: str) -> bool:
    if not pattern:
        return False
    if os.path.isabs(pattern) or pattern.startswith("~"):
        return True
    parts = [part for part in pattern.replace("\\", "/").split("/") if part]
    return any(part == ".." or part[0] in ".[?{" for part in parts)


def _confined(roots: Sequence[Path], cwd: Path) -> str:
    base = Path(cwd).resolve()
    resolved = [Path(root).resolve() for root in roots]
    if not resolved or resolved == [base]:
        return _CONFINED
    names = []
    for root, real in zip(roots, resolved, strict=True):
        name = real.relative_to(base).as_posix() if real.is_relative_to(base) else str(root)
        names.append(f"{name}/" if real.is_dir() else name)
    return (
        f"This chat may read only: {', '.join(names)}. "
        "Pass path (Glob, Grep) or file_path (Read) inside one of them."
    )


def confinement_denial(
    tool_name: str, tool_input: Mapping[str, Any], roots: Sequence[Path], *, cwd: Path,
) -> str | None:
    """Reason to deny a file-tool call that reaches outside the read roots, or None.

    `cwd` is the session's working directory, the vault. The target is
    resolved as Claude Code resolves it: `Read` needs a `file_path`; a missing
    or empty Glob or Grep `path` is `cwd`; a relative value is joined to `cwd`,
    never to a root. The target must then lie inside one of `roots`, with
    symlinks resolved and no hidden segment below the root. A path that cannot
    be checked is refused.
    """
    if tool_name not in _BUILTIN_NAMES:
        return None
    if not roots:
        return _CONFINED
    try:
        return _confinement_reason(tool_name, tool_input, roots, cwd)
    except (OSError, RuntimeError, ValueError):
        return _CONFINED


def _confinement_reason(
    tool_name: str, tool_input: Mapping[str, Any], roots: Sequence[Path], cwd: Path,
) -> str | None:
    if tool_name == "Read":
        value = str(tool_input.get("file_path") or "")
        if not value.strip(_JS_WHITESPACE):
            return _confined(roots, cwd)
    else:
        value = str(tool_input.get("path") or "")
    target = _tool_target(value, cwd)
    if target is None or not any(_inside(root, target) for root in roots):
        return _confined(roots, cwd)
    if tool_name == "Glob" and _unsafe_pattern(str(tool_input.get("pattern") or "")):
        return _confined(roots, cwd)
    if tool_name == "Grep" and _unsafe_pattern(str(tool_input.get("glob") or "")):
        return _confined(roots, cwd)
    return None


class WebRiskBarrier:
    """Per-run state for the untrusted-input rule.

    `check` is called before every tool call with the bare tool name. It
    returns a reason when the call must be blocked. A direct writer
    (`DIRECT_MUTATORS`) is blocked after untrusted content, except in the
    owner's private place (see `_writer_allowed_after_reading`), and after web
    content everywhere. A run makes at most `MAX_WEB_SEARCHES`
    searches; in the shared place they come before any other tool. After web
    content only `ALLOWED_AFTER_WEB` and the read-only `READS_AFTER_WEB` remain,
    and once one of those reads has run, no further search may carry what it read. It also sets
    `caller_context["untrusted_input"]`, which every later brain tool call
    carries to the Python side.
    """

    def __init__(self, caller_context: dict[str, Any]):
        self._caller = caller_context
        self._web_seen = False
        self._tool_seen = False
        self._direct_mutation_tainted = bool(caller_context.get("untrusted_input"))
        self._started_untrusted = self._direct_mutation_tainted
        self._humans_lookup_seen = False
        self._searches = 0
        self._read_after_web = False

    @property
    def private(self) -> bool:
        """The owner's private place: the runner set `areas` and it is None (no area limit).

        There a direct writer stays allowed after reading mail, the vault or the
        calendar; web content still ends it below. A caller without an `areas`
        entry is treated as the shared place.
        """
        return "areas" in self._caller and self._caller["areas"] is None

    def _writer_allowed_after_reading(self, bare_name: str) -> bool:
        """In the private place a direct writer may follow untrusted reads.

        Only when the run did not start with outside content (an image, an
        attached audio file, quoted text), and in an unattended run only
        `create_draft`: a draft is never sent without the owner.
        """
        if not self.private or self._started_untrusted:
            return False
        return bool(self._caller.get("is_reply")) or bare_name in UNATTENDED_WRITERS_AFTER_READING

    @property
    def web_seen(self) -> bool:
        return self._web_seen

    @property
    def query_terms(self) -> frozenset[str]:
        return frozenset(self._caller.get("web_query_terms") or ())

    def _search_denial(self) -> str | None:
        if self._searches >= MAX_WEB_SEARCHES:
            return f"A run may make at most {MAX_WEB_SEARCHES} web searches"
        if not self.private and self._tool_seen:
            return "In the shared place, web searches must come before any other tool call"
        if self._read_after_web:
            return "No web search after reading your data that followed web content"
        return None

    def check(self, bare_name: str) -> str | None:
        is_search = bare_name == "web_search"
        is_web = is_search or bare_name == "web_read"
        if is_search:
            reason = self._search_denial()
            if reason:
                return reason
            self._searches += 1
        if not is_web:
            self._tool_seen = True
        if is_web:
            self._web_seen = True
        untrusted_output = is_web or bare_name in UNTRUSTED_OUTPUTS
        if untrusted_output:
            self._caller["untrusted_input"] = True
        if untrusted_output and bare_name == "humans_find":
            self._humans_lookup_seen = True
        elif untrusted_output:
            self._direct_mutation_tainted = True
        if bare_name in DIRECT_MUTATORS and not self._writer_allowed_after_reading(bare_name) and (
            self._direct_mutation_tainted
            or (self._humans_lookup_seen and bare_name not in HUMANS_LOOKUP_MUTATORS)
        ):
            return (
                f"{bare_name} requires a clean, text-only request; use a "
                "confirmation-gated action or ask the user to restate it"
            )
        if not is_web and self._web_seen and bare_name not in ALLOWED_AFTER_WEB:
            if bare_name not in READS_AFTER_WEB:
                return f"{bare_name} is blocked after untrusted web content entered this run"
            self._read_after_web = True
        return None


VERIFY_BLOCKED_EFFECTS = frozenset({
    "action_note_complete", "complete_reminder", "create_calendar_event",
    "create_contact", "create_reminder", "mail_archive", "mail_archive_batch",
    "tasks_create", "update_calendar_event", "update_contact",
})


def verify_denial(bare_name: str) -> str | None:
    if bare_name in DIRECT_MUTATORS or bare_name in VERIFY_BLOCKED_EFFECTS:
        return f"{bare_name} is blocked during a verification run"
    return None


_FALSY = {"0", "false", "no", "off"}


def feature_on(name: str, environ: Mapping[str, str]) -> bool:
    """True unless the switch is set to an off value; an unset switch is on."""
    return (environ.get(name) or "").strip().lower() not in _FALSY


FEATURE_TOOLS: dict[str, frozenset[str]] = {
    "BRAIN_MEDIA": frozenset({"draw_chart", "draw_image"}),
    "BRAIN_FOLLOWUPS": frozenset({
        "follow_ups_digest", "follow_ups_list", "follow_ups_add", "follow_ups_update", "night_report",
        "follow_ups_open", "remind_me",
    }),
}


def feature_denial(bare_name: str, environ: Mapping[str, str]) -> str | None:
    for switch, tools in FEATURE_TOOLS.items():
        if bare_name in tools and not feature_on(switch, environ):
            return f"{bare_name} is off ({switch}=0)"
    return None


OUTSIDE_CONTENT_TOOLS = frozenset({
    "mail_search", "mail_get_message", "mail_list_attachments", "mail_get_attachment",
    "calendar_list_events", "calendar_get_event",
    "contacts_search", "contacts_get",
    "web_search", "web_read",
    "intake_pending", "search_houses", "process_watches", "public_discovery_candidates",
})


class OutsideContentState:
    """Per-run state for the direct-edit rule: has outside content entered the run?

    `note_tool` is called after a tool call is allowed, for the run and for its
    helpers alike. It also sets `caller_context["outside_content"]`, which every
    later brain tool call carries to the Python side. `note_result` takes a tool
    result the Python side flagged (a finding stored by a run that had seen
    outside content) and sets `caller_context["outside_content"]` the same way,
    so what the run stores next is flagged too. Only in an unattended run does
    it also end direct edits: in a chat run the owner is present, so direct
    edits stay allowed and `blocks_edits` stays false.
    """

    def __init__(self, caller_context: dict[str, Any], *, unattended: bool, seen: bool = False):
        self._caller = caller_context
        self._unattended = unattended
        self._seen = seen

    @property
    def seen(self) -> bool:
        return self._seen

    @property
    def blocks_edits(self) -> bool:
        return self._unattended and self._seen

    def note_tool(self, bare_name: str) -> None:
        if bare_name in OUTSIDE_CONTENT_TOOLS:
            self._seen = True
            self._caller["outside_content"] = True

    def note_result(self, result: Mapping[str, Any]) -> None:
        if not result.get("outside_content"):
            return
        self._caller["outside_content"] = True
        if self._unattended:
            self._seen = True


_EDIT_TOOLS = ("Edit", "Write")
_WRITE_LIMIT = 256 * 1024
_SHRINK_FLOOR = 0.75

_EDIT_AFTER_OUTSIDE = (
    "Direct edits are off in this run because it has read outside content; "
    "use propose_action with edit_vault_file so the owner can approve the change"
)


def _parts_below(path: Path, *roots: Path) -> tuple[str, ...] | None:
    for root in roots:
        if path.is_relative_to(root):
            return path.relative_to(root).parts
    return None


def _crosses_symlink(root: Path, parts: Sequence[str]) -> bool:
    """True when a component of `root/parts` is a symlink, a dangling one included.

    Resolving the path would miss a dangling link, and a write through it
    creates the file it points at.
    """
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _write_denial(target: Path, tool_input: Mapping[str, Any]) -> str | None:
    content = str(tool_input.get("content") or "")
    if len(content.encode()) > _WRITE_LIMIT:
        return "Write content is over 256 KB; write a shorter file"
    if target.exists():
        return "Write only creates files and this one exists; change it with Edit"
    if not target.parent.is_dir():
        return "Write does not create folders and the folder for this file does not exist"
    return None


# Claude Code's Edit retries an old_string it cannot find with these curly
# quotes folded to straight ones, in the file and in old_string alike.
_STRAIGHT_QUOTES = str.maketrans({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'})
_BYTE_ORDER_MARK = chr(0xFEFF)
# Outside Markdown, Claude Code drops JavaScript's whitespace from the end of each new_string line.
_LINE_END_SPACE = _JS_WHITESPACE + "".join(
    char for char in map(chr, range(0x3001)) if char.isspace()
)
_LINE_BREAK = re.compile(r"(\r\n|\n|\r)")


def _edit_text(raw: bytes) -> str | None:
    """Claude Code's Edit reads a file as UTF-16 LE when it starts with the byte-order mark
    `FF FE`, else as UTF-8, drops a leading byte-order mark and turns CRLF into LF.
    """
    try:
        text = raw.decode("utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-8")
    except UnicodeDecodeError:
        return None
    return text.removeprefix(_BYTE_ORDER_MARK).replace("\r\n", "\n")


def _edit_match(text: str, old: str) -> str | None:
    if old in text:
        return old
    start = text.translate(_STRAIGHT_QUOTES).find(old.translate(_STRAIGHT_QUOTES))
    return text[start:start + len(old)] if start >= 0 else None


def _written_new(new: str, target: Path) -> str:
    if target.suffix.lower() in (".md", ".mdx"):
        return new
    parts = _LINE_BREAK.split(new)
    return "".join(
        part if index % 2 else part.rstrip(_LINE_END_SPACE) for index, part in enumerate(parts)
    )


def _shrink_denial(target: Path, tool_input: Mapping[str, Any]) -> str | None:
    old = str(tool_input.get("old_string") or "")
    new = _written_new(str(tool_input.get("new_string") or ""), target)
    new_bytes, old_bytes = len(new.encode()), len(old.encode())
    text = _edit_text(target.read_bytes())
    if text is None:
        return "the file is not text this rule can measure"
    match = _edit_match(text, old)
    if match is None:
        return "old_string was not found in the file; re-read it and try again"
    if match != old:
        old_bytes = len(match.encode())
    occurrences = text.count(match) if tool_input.get("replace_all") else 1
    size = len(text.encode())
    new_size = size - occurrences * old_bytes + occurrences * new_bytes
    if size > 0 and new_size < _SHRINK_FLOOR * size:
        return (
            "This edit would remove more than a quarter of the file; "
            "larger removals need an approved edit action"
        )
    return None


def _edit_path_denial(
    tool_name: str, tool_input: Mapping[str, Any], vault_root: Path,
    edit_paths: Sequence[str], offlimits: Sequence[str],
) -> str | None:
    configured = Path(os.path.abspath(vault_root))
    root = configured.resolve()
    given = str(tool_input.get("file_path") or "")
    # Claude Code resolves a relative path against the session's working directory, the vault.
    target = _tool_target(given, root)
    if target is None:
        return "write the path as Claude Code sees it, relative to the vault or absolute"
    parts = _parts_below(target, root, configured)
    if parts is None:
        return f"File path {given!r} is not inside the vault"
    if any(part.startswith(".") for part in parts):
        return "Direct edits never touch hidden files or folders"
    if _crosses_symlink(root, parts):
        return "Direct edits never follow a symlink"
    relative = "/".join(parts) or "."
    if any(names_path(parts, entry, fold=True) for entry in offlimits):
        return (
            f"{relative} is off limits for direct edits "
            "(daily notes, rule files, lessons and prompt-loaded files)"
        )
    if not path_in_vault_paths(relative, edit_paths):
        return f"{relative} is not inside the folders and files this run may edit directly"
    target = root.joinpath(*parts)
    if tool_name == "Write":
        return _write_denial(target, tool_input)
    # Claude Code's Edit creates a missing file when old_string is empty.
    if not target.is_file():
        return "Edit changes existing files only; create a new file with Write"
    return _shrink_denial(target, tool_input)


def edit_denial(
    tool_name: str, tool_input: Mapping[str, Any], *, vault_root: Path,
    edit_paths: Sequence[str], offlimits: Sequence[str], state: OutsideContentState,
) -> str | None:
    """Reason to deny a direct `Edit` or `Write`, or None.

    An unattended run that has seen outside content gets no direct edits.
    Otherwise the file must be inside the vault, not hidden, not behind a
    symlink, not in `offlimits` and inside `edit_paths`. A `Write` may only
    create a file; an `Edit` changes an existing file and may not shrink it by
    more than a quarter. The shrink is measured on the text Claude Code's Edit
    works on: the decoded file with CRLF as LF, `old_string` found exactly or
    with curly quotes folded, and `new_string` as Claude Code writes it. A file
    that does not decode, or an `old_string` found neither way, is refused,
    and so is any call whose file cannot be checked.
    """
    if tool_name not in _EDIT_TOOLS:
        return None
    if state.blocks_edits:
        return _EDIT_AFTER_OUTSIDE
    try:
        return _edit_path_denial(tool_name, tool_input, vault_root, edit_paths, offlimits)
    except (OSError, RuntimeError, ValueError) as exc:
        return f"The file could not be checked ({type(exc).__name__}), so the edit was refused"


def vault_offlimits(vault_instructions: Mapping[str, Any], daily_notes_dir: str) -> list[str]:
    """Vault paths no direct edit may touch, as the `offlimits` argument of `edit_denial`.

    The daily notes folder, the vault-wide rule files and lessons, and every
    path the merged `vault_instructions` config allows an agent to load as
    standing guidance. Folders end in `/`.
    """
    daily = daily_notes_dir.strip().strip("/")
    agents = vault_instructions.get("agents") or {}
    paths = [
        *([f"{daily}/"] if daily else []),
        "CLAUDE.md", "VAULT_CONVENTIONS.md", "Instructions/", "Brain Agents/",
        *(vault_instructions.get("allowed_roots") or []),
        *(path for files in agents.values() for path in files or []),
    ]
    return list(dict.fromkeys(paths))


HELPER_TYPE = "helper"
HELPER_BUILTINS = ("Read", "Glob", "Grep")
HELPER_DENIED = (
    "Agent", "Task", "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "WebSearch", "WebFetch", "Skill",
)
HELPER_READ_TOOLS = (OUTSIDE_CONTENT_TOOLS - {"web_search", "web_read"}) | frozenset({
    "get_recent_findings", "list_pending_actions", "search_knowledge",
    "reminders_list", "reminders_get", "tasks_list",
    "humans_find", "humans_read", "humans_due", "humans_open_threads", "humans_meetings",
    "occasions_scan", "occasions_get_dossier", "garmin_daily", "garmin_trends",
    "list_watched_searches",
    "ops_summary", "area_notes", "list_vault_edits",
})


class HelperCap:
    """Per-run limit on helper starts, enforced here so a document cannot raise it.

    `check` is called before every tool call. It only concerns the `Agent`
    tool, which the CLI calls `Task`: the call must ask for the helper type,
    and every allowed call counts toward `limit`.
    """

    def __init__(self, limit: int):
        self._limit = limit
        self.started = 0

    def check(self, tool_name: str, tool_input: Mapping[str, Any]) -> str | None:
        if tool_name not in ("Agent", "Task"):
            return None
        if tool_input.get("subagent_type") != HELPER_TYPE:
            return f"Only the {HELPER_TYPE} agent type may be started"
        if self.started >= self._limit:
            return f"This run may start at most {self._limit} helpers"
        self.started += 1
        return None


def helper_tool_denial(hook_input: Mapping[str, Any], allowed: set[str]) -> str | None:
    """Reason to deny a tool call made inside a helper that its definition does not list.

    The SDK fires the session's hooks for a helper's tool calls too; those
    inputs carry an `agent_id`. A call without one belongs to the run itself.
    """
    if not hook_input.get("agent_id"):
        return None
    tool_name = str(hook_input.get("tool_name") or "")
    if tool_name in allowed:
        return None
    return f"{bare_tool_name(tool_name)} is not available to a helper"


_SECRET_NAME = re.compile(r"(_TOKEN|_PASSWORD|_KEY|_SECRET|_CREDENTIALS(_PATH)?)$")


def scrubbed_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The daemon's secret variables set to empty, to override them in the Claude session.

    Names ending in a secret suffix are blanked, except the session's own
    `CLAUDE...` sign-in variables.
    """
    return {
        name: "" for name in environ
        if _SECRET_NAME.search(name) and not name.startswith("CLAUDE")
    }
