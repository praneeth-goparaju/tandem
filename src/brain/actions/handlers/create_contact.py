from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from src.brain.actions.outcomes import (
    failure_from_exception,
    is_retryable_http_status,
    retryable_failure,
)

_VCARD_TYPE_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")


def is_vcard_type_token(value: object) -> bool:
    return isinstance(value, str) and bool(_VCARD_TYPE_RE.fullmatch(value))


def _escape_vcard_value(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
         .replace("\r\n", "\n")
         .replace("\r", "\n")
         .replace(";", "\\;")
         .replace(",", "\\,")
         .replace("\n", "\\n")
    )


def _emit_typed_lines(field: str, items) -> list[str]:
    lines: list[str] = []
    for it in items or []:
        if isinstance(it, dict):
            value = (it.get("value") or "").strip()
            kinds = it.get("type")
            if not value:
                continue
            type_str = ""
            if kinds:
                tokens = [kinds] if isinstance(kinds, str) else kinds
                if not isinstance(tokens, list) or not all(
                    is_vcard_type_token(token) for token in tokens
                ):
                    raise ValueError("invalid vCard TYPE token")
                type_str = ";" + ",".join(f"TYPE={token.upper()}" for token in tokens)
            lines.append(f"{field}{type_str}:{_escape_vcard_value(value)}")
        elif isinstance(it, str) and it.strip():
            lines.append(f"{field}:{_escape_vcard_value(it.strip())}")
    return lines


def _build_vcard(payload: dict[str, Any], uid: str) -> str:
    fn = payload["full_name"].strip()
    family = (payload.get("family_name") or "").strip()
    given = (payload.get("given_name") or "").strip()
    if not (family or given) and " " in fn:
        given, family = fn.split(" ", 1)

    n_components = ";".join(_escape_vcard_value(c) for c in (family, given, "", "", ""))

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"UID:{uid}",
        f"FN:{_escape_vcard_value(fn)}",
        f"N:{n_components}",
    ]
    lines.extend(_emit_typed_lines("EMAIL", payload.get("emails")))
    lines.extend(_emit_typed_lines("TEL", payload.get("tels")))
    if payload.get("org"):
        lines.append(f"ORG:{_escape_vcard_value(payload['org'])}")
    if payload.get("title"):
        lines.append(f"TITLE:{_escape_vcard_value(payload['title'])}")
    if payload.get("note"):
        lines.append(f"NOTE:{_escape_vcard_value(payload['note'])}")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def _execute_sync(payload: dict[str, Any]) -> dict[str, Any]:
    from src.brain.mcp.tools._icloud import (
        ICloudNotConfigured,
        assert_icloud_host,
        carddav_addressbook_url,
        carddav_http_client,
    )

    if not (payload.get("full_name") or "").strip():
        return {"ok": False, "result": "missing full_name"}

    action_id = str(payload.get("_action_id") or "").strip()
    uid_value = (
        uuid.uuid5(uuid.NAMESPACE_URL, f"brain-action:{action_id}")
        if action_id
        else uuid.uuid4()
    )
    uid = str(uid_value).upper()
    try:
        vcard = _build_vcard(payload, uid)
    except ValueError as exc:
        return {"ok": False, "result": str(exc)}

    try:
        with carddav_http_client() as client:
            ab = carddav_addressbook_url(client)
            target = assert_icloud_host(f"{ab.rstrip('/')}/{uid}.vcf")
            resp = client.put(
                target,
                content=vcard,
                headers={
                    "Content-Type": "text/vcard; charset=utf-8",
                    "If-None-Match": "*",
                },
            )
    except ICloudNotConfigured as e:
        return {"ok": False, "result": str(e)}
    except Exception as e:  # noqa: BLE001
        return failure_from_exception("CardDAV PUT failed", e)

    if resp.status_code in (412,):
        if action_id:
            return {
                "ok": True,
                "result": f"contact uid={uid} already exists (idempotent retry)",
            }
        return {"ok": False, "result": f"contact uid={uid} already exists (use update_contact)"}
    if resp.status_code not in (200, 201, 204):
        reason = f"CardDAV PUT returned {resp.status_code}: {resp.text[:200]}"
        if is_retryable_http_status(resp.status_code):
            return retryable_failure(reason)
        return {"ok": False, "result": reason}
    return {"ok": True, "result": f"created contact uid={uid} href={target}"}


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_execute_sync, payload)
