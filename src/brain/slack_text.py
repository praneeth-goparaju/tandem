from __future__ import annotations

import unicodedata


def sanitize_slack_text(text: str) -> str:
    safe: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char in "\n\t\u200c\u200d" or (
            not category.startswith("C") and category not in {"Zl", "Zp"}
        ):
            safe.append(char)
    return "".join(safe)
