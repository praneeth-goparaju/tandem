from __future__ import annotations

import re

_REDACTED = "[redacted]"

_RULES = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        _REDACTED,
    ),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.DOTALL), _REDACTED),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]*:)[^\s/@]+@"), rf"\1{_REDACTED}@"),
    (re.compile(r"hooks\.slack\.com/services/\S+"), _REDACTED),
    (re.compile(r"\bsk_live_[A-Za-z0-9]{8,}"), _REDACTED),
    (re.compile(r"GOCSPX-[A-Za-z0-9_-]{8,}"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), _REDACTED),
    (re.compile(r"xox[a-z]-\S+"), _REDACTED),
    (re.compile(r"xapp-\S+"), _REDACTED),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), _REDACTED),
    (re.compile(r"AKIA[0-9A-Z]{16}"), _REDACTED),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), _REDACTED),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), _REDACTED),
    (re.compile(r"(?i)--password[ =]\S+"), _REDACTED),
    (
        re.compile(
            r"(?i)(?<![a-z])authorization[\"']?[ \t]*[=:][ \t]*[\"']?"
            r"(?:(?:bearer|basic|token)[ \t]+)?[^\s\"']+[\"']?"
        ),
        _REDACTED,
    ),
    (
        re.compile(
            r"(?i)(?<![a-z])(password|passwd|pwd|passphrase|token|secret|api[_-]?key"
            r"|access[_-]?key|client[_-]?secret)[\"']?[ \t]*[=:][ \t]*"
            r"(\"[^\"\n]*\"|'[^'\n]*'|\S+)"
        ),
        _REDACTED,
    ),
    (re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+]{32,}={0,2}(?![A-Za-z0-9+/])"), _REDACTED),
    (re.compile(r"(?<![A-Za-z0-9+/.])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])"), _REDACTED),
    (
        re.compile(
            r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]{32,}(?![A-Za-z0-9+/]))"
            r"[A-Za-z0-9/]*\+[A-Za-z0-9+/]*"
        ),
        _REDACTED,
    ),
    (re.compile(r"\b[0-9a-f]{32,}\b"), _REDACTED),
)


def scrub_secrets(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
