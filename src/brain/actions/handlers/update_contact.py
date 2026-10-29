from __future__ import annotations

import asyncio
from typing import Any

from src.brain.actions.handlers.create_contact import _escape_vcard_value
from src.brain.actions.outcomes import (
    failure_from_exception,
    is_retryable_http_status,
    retryable_failure,
)
from src.brain.mcp.tools._icloud import assert_icloud_host


def _patch_vcard(vcard, payload: dict[str, Any]):
    import vobject

    if "full_name" in payload:
        fn_value = _escape_vcard_value(payload["full_name"])
        if hasattr(vcard, "fn"):
            vcard.fn.value = fn_value
        else:
            vcard.add("fn").value = fn_value

    if "family_name" in payload or "given_name" in payload:
        family_in = payload.get("family_name")
        given_in = payload.get("given_name")
        if hasattr(vcard, "n") and vcard.n.value is not None:
            n = vcard.n.value
            family = family_in if family_in is not None else getattr(n, "family", "") or ""
            given = given_in if given_in is not None else getattr(n, "given", "") or ""
        else:
            family = family_in or ""
            given = given_in or ""
        n_obj = vobject.vcard.Name(
            family=_escape_vcard_value(family),
            given=_escape_vcard_value(given),
        )
        if hasattr(vcard, "n"):
            vcard.n.value = n_obj
        else:
            vcard.add("n").value = n_obj

    if "set_emails" in payload:
        for e in list(getattr(vcard, "email_list", [])):
            vcard.remove(e)
        for value in payload["set_emails"]:
            vcard.add("email").value = _escape_vcard_value(value)
    elif "add_emails" in payload:
        for value in payload["add_emails"]:
            vcard.add("email").value = _escape_vcard_value(value)

    if "set_tels" in payload:
        for t in list(getattr(vcard, "tel_list", [])):
            vcard.remove(t)
        for value in payload["set_tels"]:
            vcard.add("tel").value = _escape_vcard_value(value)
    elif "add_tels" in payload:
        for value in payload["add_tels"]:
            vcard.add("tel").value = _escape_vcard_value(value)

    for field in ("org", "title"):
        if field in payload:
            value = _escape_vcard_value(payload[field])
            if hasattr(vcard, field):
                getattr(vcard, field).value = value
            else:
                vcard.add(field).value = value

    if "note" in payload:
        note_value = _escape_vcard_value(payload["note"])
        if hasattr(vcard, "note"):
            vcard.note.value = note_value
        else:
            vcard.add("note").value = note_value
    elif "note_append" in payload:
        existing = vcard.note.value if hasattr(vcard, "note") else ""
        appended = _escape_vcard_value(payload["note_append"])
        new = (existing + ("\n" if existing else "") + appended).strip()
        if hasattr(vcard, "note"):
            vcard.note.value = new
        else:
            vcard.add("note").value = new


def _execute_sync(payload: dict[str, Any]) -> dict[str, Any]:
    import vobject

    from src.brain.mcp.tools._icloud import (
        ICLOUD_CARDDAV_URL,
        ICloudNotConfigured,
        _join,
        carddav_http_client,
    )

    href = (payload.get("href") or "").strip()
    if not href:
        return {"ok": False, "result": "missing href"}

    try:
        with carddav_http_client() as client:
            url = assert_icloud_host(_join(ICLOUD_CARDDAV_URL, href))
            for attempt in (1, 2):
                resp = client.get(url, headers={"Accept": "text/vcard"})
                if resp.status_code != 200:
                    reason = f"CardDAV GET {url} returned {resp.status_code}"
                    if is_retryable_http_status(resp.status_code):
                        return retryable_failure(reason)
                    return {"ok": False, "result": reason}
                etag = resp.headers.get("ETag", "")
                try:
                    vcard = vobject.readOne(resp.text)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "result": f"vCard parse failed: {e}"}
                _patch_vcard(vcard, payload)
                put = client.put(
                    url,
                    content=vcard.serialize(),
                    headers={
                        "Content-Type": "text/vcard; charset=utf-8",
                        **({"If-Match": etag} if etag else {}),
                    },
                )
                if put.status_code == 412 and attempt == 1:
                    continue
                if put.status_code in (200, 201, 204):
                    return {"ok": True, "result": f"updated contact href={href}"}
                reason = f"CardDAV PUT returned {put.status_code}: {put.text[:200]}"
                if is_retryable_http_status(put.status_code):
                    return retryable_failure(reason)
                return {"ok": False, "result": reason}
    except ICloudNotConfigured as e:
        return {"ok": False, "result": str(e)}
    except Exception as e:  # noqa: BLE001
        return failure_from_exception("CardDAV update failed", e)

    raise AssertionError("for-loop should always return")


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_execute_sync, payload)
