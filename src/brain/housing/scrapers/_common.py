from __future__ import annotations

import re

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=HEADERS, timeout=20, follow_redirects=False, trust_env=False
    )


_PRICE_RE = re.compile(r"([\d.]+)")


def parse_price_eur(text: str | None) -> int | None:
    if not text:
        return None
    digits = _PRICE_RE.findall(text.replace(" ", " "))
    if not digits:
        return None
    num = digits[0].replace(".", "").replace(",", "")
    try:
        val = int(num)
    except ValueError:
        return None
    return val if val >= 1000 else None


_POSTCODE_RE = re.compile(r"\b(\d{5})\b")


def extract_postcode(text: str | None) -> str:
    if not text:
        return ""
    m = _POSTCODE_RE.search(text)
    return m.group(1) if m else ""
