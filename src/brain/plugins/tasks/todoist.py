from __future__ import annotations

import logging
from typing import Any

import httpx

from src.brain.actions.outcomes import is_retryable_http_status
from src.brain.plugins.base import TaskCreateOutcomeUnknown

log = logging.getLogger("brain.plugins.tasks.todoist")

_BASE = "https://api.todoist.com/api/v1"


def _token() -> str:
    from src.orchestrator.config import TODOIST_API_TOKEN

    if not TODOIST_API_TOKEN:
        raise RuntimeError(
            "TODOIST_API_TOKEN not configured — set it in .env or switch "
            "config/integrations.yaml to `tasks: noop`."
        )
    return TODOIST_API_TOKEN


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)


class TodoistTaskManager:
    async def add(
        self,
        title: str,
        *,
        description: str = "",
        project: str | None = None,
        priority: int | None = None,
        due: str | None = None,
        labels: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": title}
        if description:
            payload["description"] = description
        if project:
            payload["project_id"] = project
        if priority:
            payload["priority"] = priority
        if due:
            payload["due_string"] = due
        if labels:
            payload["labels"] = labels

        headers = _headers()
        if idempotency_key:
            # Todoist documents X-Request-Id (max 36 bytes) as its durable
            # POST deduplication key. Queue handlers pass a deterministic UUID.
            if len(idempotency_key.encode("utf-8")) > 36:
                raise ValueError("Todoist idempotency_key must not exceed 36 bytes")
            headers["X-Request-Id"] = idempotency_key

        async with _client() as client:
            try:
                resp = await client.post(f"{_BASE}/tasks", headers=headers, json=payload)
            except httpx.TransportError as exc:
                if not idempotency_key:
                    raise TaskCreateOutcomeUnknown(
                        f"Todoist transport failed after create dispatch: {exc}"
                    ) from exc
                raise
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if not idempotency_key and is_retryable_http_status(resp.status_code):
                    raise TaskCreateOutcomeUnknown(
                        f"Todoist returned {resp.status_code} after unkeyed create"
                    ) from exc
                raise
            data = resp.json()
            return {"id": str(data.get("id")), "url": data.get("url")}

    async def list_pending(self, *, project: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if project:
            params["project_id"] = project
        async with _client() as client:
            resp = await client.get(f"{_BASE}/tasks", headers=_headers(), params=params)
            resp.raise_for_status()
            return resp.json().get("results", resp.json()) or []

    async def list_completed(
        self, *, since_hours: int = 24, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with _client() as client:
            resp = await client.get(
                f"{_BASE}/tasks/completed",
                headers=_headers(),
                params={"limit": limit},
            )
            resp.raise_for_status()
            return resp.json().get("items", [])
