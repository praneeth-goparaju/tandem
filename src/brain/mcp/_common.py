from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {
        "is_error": True,
        "content": [{"type": "text", "text": f"ERROR: {text}"}],
    }


def _bounded_int(
    value: object, *, default: int, minimum: int, maximum: int
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"must be an integer between {minimum} and {maximum}")
    return value


_EXTERNAL_TAG_RE = re.compile(r"<\s*/?\s*external[_-]content\b[^>]*>", re.IGNORECASE)


def _wrap_external(content: str, source: str) -> str:
    if not content:
        return ""
    safe = _EXTERNAL_TAG_RE.sub("[external-content]", content)
    safe_source = (
        str(source)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
        .replace("\r", " ")
    )
    return f'<external_content source="{safe_source}">\n{safe}\n</external_content>'


def _bracket(s) -> str:
    if s is None:
        return "[]"
    text = _EXTERNAL_TAG_RE.sub("[external-content]", str(s))
    text = text.replace("[", "(").replace("]", ")")
    text = text.replace("\r", " ").replace("\n", " ")
    return f"[{text}]"


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@lru_cache(maxsize=1)
def integrations_config() -> dict:
    from src.brain.runtime.loader import load_integrations, resolve_user_dir

    return load_integrations(_REPO_ROOT / "config", resolve_user_dir(_REPO_ROOT))


@lru_cache(maxsize=1)
def notifier():
    from src.brain.plugins.loader import load_notifier

    return load_notifier(integrations_config())


@lru_cache(maxsize=1)
def tasks():
    from src.brain.plugins.loader import load_tasks

    return load_tasks(integrations_config())
