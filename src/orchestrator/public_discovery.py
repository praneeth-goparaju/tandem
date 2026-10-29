from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.brain.atomic_io import read_regular_text
from src.brain.net_security import safe_https_url

_SEARCH_URL = "https://api.tavily.com/search"
_MAX_RESPONSE_BYTES = 1_000_000
_TIME_RANGE = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}
# The provider's `country` parameter takes a lowercase English name and only
# applies to the general topic, where it boosts results from that country.
_COUNTRY_NAMES = {
    "at": "austria", "ch": "switzerland", "de": "germany", "fr": "france",
    "gb": "united kingdom", "in": "india", "nl": "netherlands", "us": "united states",
}
_DOMAIN_RE = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"


class PublicQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,49}$")
    query: str = Field(min_length=1, max_length=400)
    country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    search_lang: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,5}$")
    topic: Literal["general", "news"] = "general"
    freshness: Literal["pd", "pw", "pm", "py"] | None = None
    include_domains: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("include_domains")
    @classmethod
    def _validate_domains(cls, value: list[str]) -> list[str]:
        bad = [d for d in value if not re.fullmatch(_DOMAIN_RE, d)]
        if bad:
            raise ValueError(f"include_domains must be bare hostnames: {bad}")
        return value


class PublicDiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    enabled: bool = False
    schedule: str = "35 */3 * * *"
    max_queries_per_run: int = Field(default=4, ge=1, le=20)
    results_per_query: int = Field(default=6, ge=1, le=10)
    queries: list[PublicQuery] = Field(default_factory=list, max_length=200)


def public_config_path(repo_root: Path) -> Path:
    user_dir = Path(
        os.path.abspath(
            Path(os.getenv("USER_DIR", str(repo_root / "user"))).expanduser()
        )
    )
    return user_dir / "public_discovery.yaml"


def load_config(repo_root: Path) -> PublicDiscoveryConfig:
    path = public_config_path(repo_root)
    try:
        raw = yaml.safe_load(read_regular_text(path)) or {}
    except FileNotFoundError:
        return PublicDiscoveryConfig()
    return PublicDiscoveryConfig.model_validate(raw)


def selected_queries(
    config: PublicDiscoveryConfig,
    *,
    slot: int | None = None,
) -> list[PublicQuery]:
    if not config.queries:
        return []
    if slot is None:
        slot = int(datetime.now(UTC).timestamp() // (3 * 60 * 60))
    count = min(config.max_queries_per_run, len(config.queries))
    start = (slot * count) % len(config.queries)
    return [config.queries[(start + offset) % len(config.queries)] for offset in range(count)]


def _candidate_source_id(query_id: str, candidate: dict) -> str:
    stable = "\n".join(
        str(candidate.get(field) or "")
        for field in ("url", "title", "description")
    )
    digest = hashlib.sha256(f"{query_id}\n{stable}".encode()).hexdigest()[:32]
    return f"public-discovery:{digest}"


def _parse_results(
    payload: object, query: PublicQuery, result_count: int
) -> list[dict]:
    if not isinstance(payload, dict):
        raise RuntimeError("search provider returned an invalid response")
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = safe_https_url(row.get("url"))
        if not url:
            continue
        candidates.append({
            "query_id": query.id,
            "query": query.query,
            "title": str(row.get("title") or "")[:500],
            "url": url,
            "description": str(row.get("content") or "")[:2000],
            "age": str(row.get("published_date") or "")[:100],
        })
        if len(candidates) >= result_count:
            break
    return candidates


async def tavily_search(
    query: PublicQuery,
    *,
    result_count: int,
    token: str,
    client: httpx.AsyncClient,
) -> list[dict]:
    payload: dict[str, str | int | bool | list[str]] = {
        "query": query.query,
        "search_depth": "basic",
        "max_results": result_count,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    if query.freshness:
        payload["time_range"] = _TIME_RANGE[query.freshness]
    if query.topic == "news":
        payload["topic"] = "news"
    country = _COUNTRY_NAMES.get((query.country or "").lower())
    if country and query.topic == "general":
        payload["country"] = country
    if query.include_domains:
        payload["include_domains"] = list(query.include_domains)
    async with client.stream(
        "POST",
        _SEARCH_URL,
        json=payload,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {token}",
        },
    ) as response:
        if response.status_code == 429:
            raise RuntimeError("web search rate limit reached")
        if response.status_code in {401, 403}:
            raise RuntimeError("web search credential was rejected")
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdecimal() and int(declared) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("web search response exceeded 1MB")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("web search response exceeded 1MB")
            body.extend(chunk)
    try:
        response_payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("web search returned invalid JSON") from exc
    return _parse_results(response_payload, query, result_count)


async def run_once(
    config: PublicDiscoveryConfig,
    *,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
    slot: int | None = None,
) -> dict[str, int]:
    if not config.enabled:
        return {"queries": 0, "candidates": 0}
    if not config.queries:
        raise RuntimeError("public discovery is enabled but has no public queries")
    credential = (token if token is not None else os.getenv("TAVILY_API_KEY", "")).strip()
    if not credential:
        raise RuntimeError("TAVILY_API_KEY is not configured")

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
    inserted = 0
    queries = selected_queries(config, slot=slot)
    try:
        for query in queries:
            results = await tavily_search(
                query,
                result_count=config.results_per_query,
                token=credential,
                client=client,
            )
            from src.memory import store

            for candidate in results:
                content = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
                intake_id = store.intake_add(
                    source="public-discovery",
                    source_id=_candidate_source_id(query.id, candidate),
                    kind="web_candidate",
                    content=content,
                    meta={"query_id": query.id, "url": candidate["url"]},
                )
                if intake_id is not None:
                    inserted += 1
                    store.log_event(
                        actor="public_discovery",
                        action="candidate_ingested",
                        subject=intake_id,
                        trust="untrusted",
                        payload=content,
                        detail=f"query_id={query.id}",
                    )
    finally:
        if own_client:
            await client.aclose()
    return {"queries": len(queries), "candidates": inserted}
