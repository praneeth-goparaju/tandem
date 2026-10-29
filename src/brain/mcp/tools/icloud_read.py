"""Read tools for iCloud Calendar / Mail / Reminders / Contacts.

Pure read tools — agents call them to enrich their context. Calendar,
reminders, contacts, and mail archive effects go through `propose_action` and
the audited action queue. The trusted local interactive MCP profile may execute
a selected archive immediately; scheduled and tainted callers remain gated.
Reply composition is the exception: `mail_reply_link` (in `icloud_actions.py`)
bypasses the queue without sending mail itself.

Surfaces:
  - `calendar_list_events`  — events in a date window across user's calendars
  - `calendar_get_event`    — full event detail by URL
  - `mail_search`           — IMAP search by query / since / mailbox
  - `mail_get_message`      — fetch one message by UID + mailbox
  - `reminders_list`        — Reminders entries in one or all lists
  - `reminders_get`         — single reminder by EventKit identifier
  - `contacts_search`       — CardDAV contacts by free-text query
  - `contacts_get`          — full vCard by href

Conventions:
  - All tools return `{"content": [{"type": "text", "text": <str>}]}`.
  - Errors are returned as text with an `ERROR:` prefix; we never raise out
    of a tool body so the agent always gets something coherent back.
  - Date inputs are ISO 8601. Outputs are ISO 8601 in the user's timezone
    where Apple gave us tz info; otherwise UTC.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, cast
from zoneinfo import ZoneInfo

from src.brain.mcp._common import _bounded_int, _bracket, _err, _ok, _wrap_external

_log = logging.getLogger(__name__)

from src.brain.mcp.tool_registry import brain_tool
from src.brain.mcp.tools._icloud import (
    CARDDAV_NS,
    ICloudNotConfigured,
    assert_icloud_host,
    caldav_client,
    carddav_addressbook_url,
    carddav_http_client,
    escape_imap_str,
    eventkit_store,
    validate_mailbox_name,
    validate_single_uid,
)
from src.brain.mcp.tools._icloud import (
    imap_session as _icloud_imap_session,
)
from src.brain.mcp.tools._mail_providers import (
    MailNotConfigured,
)
from src.brain.mcp.tools._mail_providers import (
    get_provider as _get_provider,
)
from src.brain.mcp.tools._mail_providers import (
    imap_session as _generic_imap_session,
)
from src.brain.mcp.tools.mail_ops import decode_header as _decode_header
from src.brain.mcp.tools.mail_ops import fetch_raw_message


def _session_for(provider: str = "icloud"):
    """Return the IMAP session context manager for the given provider.

    iCloud keeps its dedicated session (unchanged behavior); any other
    provider goes through the generic MailProvider session.
    """
    if provider == "icloud":
        return _icloud_imap_session()
    return _generic_imap_session(_get_provider(provider))


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEFAULT_MAILBOX = "INBOX"
_MAX_MAIL_HITS = 50
_MAX_REMINDERS = 200


# _wrap_external / _bracket — the external-content envelope (prompt-injection
# defense) now live in src.brain.mcp._common so every tool wraps untrusted
# text identically; imported above.


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


# _ok / _err result envelopes now live in src.brain.mcp._common (imported above).


def _parse_date_arg(s: str | None, default: date) -> date:
    if not s:
        return default
    if not _DATE_RE.match(s):
        raise ValueError(f"date must be YYYY-MM-DD, got {s!r}")
    return date.fromisoformat(s)


def _calendar_timezone() -> tzinfo:
    """The fleet timezone, matching brief.py and the Apple Reminders plugin."""
    from src.orchestrator import config

    try:
        return ZoneInfo(str(config.TIMEZONE))
    except Exception:  # noqa: BLE001 — a bad TZ string must not block listing
        _log.warning(
            "calendar window: invalid TIMEZONE %r - falling back to UTC",
            config.TIMEZONE,
        )
        return UTC


def _local_day_window(d_from: date, d_to: date) -> tuple[datetime, datetime]:
    """Half-open [start, end) spanning local midnight on each date.

    The caller passes calendar dates and means local days. Building the window
    in UTC instead shifts it by the local offset, which at +02:00 both hides
    anything in the first two hours of ``d_from`` and lets the first two hours
    of ``d_to`` leak in - every all-day event on that date, since those begin
    at local midnight. ZoneInfo resolves the offset per date, so the window
    stays correct either side of a DST change.
    """
    tz = _calendar_timezone()
    return (
        datetime.combine(d_from, time.min, tzinfo=tz),
        datetime.combine(d_to, time.min, tzinfo=tz),
    )


@brain_tool(
    name="calendar_list_events",
    description=(
        "List iCloud calendar events in a date window. Returns one line per "
        "event with start, end, summary, calendar, and event URL (which you "
        "can pass to calendar_get_event for full detail). Defaults to the "
        "next 7 days starting today. Use `calendar_name` to scope to one "
        "calendar (e.g. 'Home', 'Work'). Reminders are NOT included — use "
        "reminders_list for those."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "date_from": {"type": "string", "description": "ISO date YYYY-MM-DD. Inclusive. Default: today."},
            "date_to": {"type": "string", "description": (
                "ISO date YYYY-MM-DD. Exclusive, except that date_to equal to date_from means that one "
                "whole day. Default: today + 7."
            )},
            "calendar_name": {"type": "string", "description": "Optional case-insensitive calendar name filter."},
            "limit": {"type": "integer", "description": "Cap on returned events. Default 100."},
        },
    },
)
async def calendar_list_events(args):
    try:
        today = date.today()
        d_from = _parse_date_arg(args.get("date_from"), today)
        d_to = _parse_date_arg(args.get("date_to"), today + timedelta(days=7))
        if d_to == d_from:
            d_to = d_from + timedelta(days=1)
        if d_to < d_from:
            return _err("date_to must not be before date_from")
        cal_filter = (args.get("calendar_name") or "").strip().lower()
        limit = int(args.get("limit") or 100)
    except (ValueError, TypeError) as e:
        return _err(str(e))

    try:
        client = caldav_client()
        principal = client.principal()
        calendars = principal.calendars()
    except ICloudNotConfigured as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001 — surface protocol errors to agent
        return _err(f"CalDAV connect failed: {type(e).__name__}: {e}")

    start_dt, end_dt = _local_day_window(d_from, d_to)

    rows: list[str] = []
    for cal in calendars:
        cal_name = (cal.name or "").strip()
        if cal_filter and cal_filter not in cal_name.lower():
            continue
        try:
            events = cal.search(start=start_dt, end=end_dt, event=True, expand=True)
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "calendar search skipped for %r: %s: %s",
                cal_name, type(e).__name__, e,
            )
            rows.append(f"[{cal_name}: (skipped: see logs)]")
            continue
        for ev in events:
            try:
                rows.append(_format_event_line(ev, cal_name))
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "malformed event in %r: %s: %s",
                    cal_name, type(e).__name__, e,
                )
                rows.append(f"[{cal_name}: malformed event (skipped: see logs)]")
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    if not rows:
        return _ok(f"No events between {d_from} and {d_to}.")
    rows.sort()
    return _ok("\n".join(rows[:limit]))


def _format_event_line(ev, cal_name: str) -> str:
    """One-line summary of a CalDAV event for the agent's context window."""
    summary, start_str, end_str = _event_summary_times(ev)
    return (
        f"{start_str} → {end_str} | {cal_name} | "
        f"{_bracket(summary)} | {getattr(ev, 'url', '')}"
    )


def _event_summary_times(ev) -> tuple[str, str, str]:
    """Read (summary, start, end) from a CalDAV event with the right parser.

    caldav returns an icalendar.Calendar from ``icalendar_instance``: its
    events come from ``walk("VEVENT")`` and its fields from ``get()``, which
    yield objects carrying ``.dt``. The ``data`` fallback is parsed by vobject,
    whose events sit on ``vevent_list`` and whose fields are attributes
    carrying ``.value``. The two APIs do not overlap, so reading one with the
    other's accessor finds nothing and every field keeps its placeholder.
    """
    summary = "(no title)"
    start_str = "?"
    end_str = "?"

    instance = getattr(ev, "icalendar_instance", None)
    if instance is not None and hasattr(instance, "walk"):
        for vevent in instance.walk("VEVENT"):
            raw_summary = vevent.get("SUMMARY")
            if raw_summary is not None:
                summary = str(raw_summary).strip() or summary
            dtstart = vevent.get("DTSTART")
            dtend = vevent.get("DTEND")
            if dtstart is not None:
                start_str = _isoformat_dt(dtstart.dt)
            if dtend is not None:
                end_str = _isoformat_dt(dtend.dt)
            break  # one event per row even on recurring expansions
        return summary, start_str, end_str

    if not hasattr(ev, "data"):
        return "(unparseable event)", start_str, end_str

    import vobject

    cal = vobject.readOne(ev.data)
    for vevent in getattr(cal, "vevent_list", []):
        summary = (getattr(vevent, "summary", None) and vevent.summary.value) or summary
        dtstart = getattr(vevent, "dtstart", None)
        dtend = getattr(vevent, "dtend", None)
        if dtstart is not None:
            start_str = _isoformat_dt(dtstart.value)
        if dtend is not None:
            end_str = _isoformat_dt(dtend.value)
        break  # one event per row even on recurring expansions
    return summary, start_str, end_str


def _isoformat_dt(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@brain_tool(
    name="calendar_get_event",
    description=(
        "Fetch full detail of a single calendar event by its URL (from the "
        "last column of calendar_list_events). Returns raw iCalendar fields "
        "(summary, location, description, attendees, organizer, recurrence)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "event_url": {"type": "string"},
        },
        "required": ["event_url"],
    },
)
async def calendar_get_event(args):
    url = (args.get("event_url") or "").strip()
    if not url:
        return _err("event_url is required")
    try:
        url = assert_icloud_host(url)
    except ValueError as e:
        return _err(str(e))
    try:
        client = caldav_client()
        # Fetch directly via DAV.
        from caldav import Event

        # caldav's dynamic export is typed as object rather than a class.
        event_cls = cast(Any, Event)
        event = event_cls(client=client, url=url)
        event.load()
    except ICloudNotConfigured as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"CalDAV fetch failed: {type(e).__name__}: {e}")

    return _ok(_format_event_full(event))


# Short sender-controlled event fields — bracket. `description` gets the
# heavier `<external_content>` wrapping below because it's free text. UID and
# RRULE are structural-looking but still arrive in attacker-controlled ICS.
_BRACKETED_EVENT_FIELDS = {"summary", "location", "organizer", "rrule", "uid"}


def _format_event_full(event) -> str:
    import vobject

    cal = vobject.readOne(event.data)
    if not hasattr(cal, "vevent_list"):
        raw = event.data
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return _wrap_external(str(raw), source="calendar/raw-event")
    parts: list[str] = []
    for vevent in cal.vevent_list:
        organizer = ""
        if hasattr(vevent, "organizer") and vevent.organizer.value:
            organizer = str(vevent.organizer.value)
        for field in ("summary", "dtstart", "dtend", "location", "description",
                      "organizer", "rrule", "uid"):
            val = getattr(vevent, field, None)
            if val is None:
                continue
            v = val.value if hasattr(val, "value") else val
            rendered = _isoformat_dt(v) if isinstance(v, (datetime, date)) else str(v)
            if field == "description":
                src = f"calendar/{organizer or 'unknown-organizer'}"
                parts.append(f"{field}:\n{_wrap_external(rendered, source=src)}")
            elif field in _BRACKETED_EVENT_FIELDS:
                parts.append(f"{field}: {_bracket(rendered)}")
            else:
                parts.append(f"{field}: {rendered}")
        attendees = []
        if hasattr(vevent, "attendee_list"):
            attendees = [a.value for a in vevent.attendee_list]
        if attendees:
            parts.append("attendees:")
            parts.extend(f"  - {_bracket(a)}" for a in attendees)
        for alarm in getattr(vevent, "valarm_list", []):
            action = getattr(getattr(alarm, "action", None), "value", "")
            trigger = getattr(getattr(alarm, "trigger", None), "value", "")
            rendered_trigger = (
                _isoformat_dt(trigger)
                if isinstance(trigger, (datetime, date))
                else str(trigger)
            )
            parts.append(f"alarm_action: {_bracket(action)}")
            parts.append(f"alarm_trigger: {_bracket(rendered_trigger)}")
            description = getattr(
                getattr(alarm, "description", None), "value", ""
            )
            if description:
                parts.append(
                    "alarm_description:\n"
                    + _wrap_external(str(description), source="calendar/alarm")
                )
        parts.append(f"url: {event.url}")
        break
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Mail (IMAP)
# ---------------------------------------------------------------------------

_MAX_MAIL_BODY_CHARS = 24_000


@brain_tool(
    name="mail_search",
    description=(
        "Search iCloud Mail by query / sender / since-date. Uses IMAP "
        "server-side search so it scales to large inboxes. Returns a list "
        "of message envelopes (UID, from, subject, date, ~120-char snippet) "
        "ordered newest-first. Pass a returned UID to mail_get_message for "
        "the full body. Default mailbox: INBOX."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["icloud", "gmail"], "description": "Mail account. Default icloud."},
            "query": {"type": "string", "description": "Free-text query — matches subject + body. Optional."},
            "from_addr": {"type": "string", "description": "Filter by sender substring (e.g. 'recruiter@', 'noreply@'). Optional."},
            "since": {"type": "string", "description": "ISO date YYYY-MM-DD. Only mail received on/after this date."},
            "mailbox": {"type": "string", "description": "IMAP folder. Default INBOX. iCloud also has 'Sent Messages', 'Drafts', 'Archive'."},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_MAIL_HITS, "description": f"Max results. Default {_MAX_MAIL_HITS}, hard cap {_MAX_MAIL_HITS}."},
            "unread_only": {"type": "boolean", "description": "If true, only unread messages."},
        },
    },
)
async def mail_search(args):
    provider = (args.get("provider") or "icloud").strip()
    query = (args.get("query") or "").strip()
    from_addr = (args.get("from_addr") or "").strip()
    since = (args.get("since") or "").strip()
    mailbox = (args.get("mailbox") or _DEFAULT_MAILBOX).strip()
    try:
        limit = _bounded_int(
            args.get("limit"), default=_MAX_MAIL_HITS,
            minimum=1, maximum=_MAX_MAIL_HITS,
        )
    except ValueError as exc:
        return _err(f"limit {exc}")
    unread_only = bool(args.get("unread_only"))

    if since and not _DATE_RE.match(since):
        return _err("since must be YYYY-MM-DD")

    try:
        mailbox = validate_mailbox_name(mailbox)
    except (TypeError, ValueError) as e:
        return _err(str(e))

    criteria: list[str] = []
    if unread_only:
        criteria.append("UNSEEN")
    if from_addr:
        try:
            from_addr_safe = escape_imap_str(from_addr)
        except (TypeError, ValueError) as e:
            return _err(f"from_addr: {e}")
        criteria.append(f'FROM "{from_addr_safe}"')
    if since:
        d = date.fromisoformat(since)
        criteria.append(f'SINCE {d.strftime("%d-%b-%Y")}')
    if query:
        # iCloud IMAP supports TEXT (matches headers + body)
        try:
            query_safe = escape_imap_str(query)
        except (TypeError, ValueError) as e:
            return _err(f"query: {e}")
        criteria.append(f'TEXT "{query_safe}"')
    if not criteria:
        criteria.append("ALL")
    search_term = " ".join(criteria)

    try:
        with _session_for(provider) as m:
            status, _ = m.select(_quote_mailbox(mailbox), readonly=True)
            if status != "OK":
                return _err(f"mailbox {mailbox!r} not found")
            # Non-ASCII queries (e.g. German umlauts in housing/job
            # searches) require CHARSET UTF-8 — bare ASCII SEARCH chokes
            # with BAD on multi-byte input. iCloud IMAP supports UTF-8
            # CHARSET; if a future server doesn't, the SEARCH will return
            # status != "OK" and the agent gets the error.
            if any(ord(c) > 127 for c in search_term):
                status, data = m.uid("SEARCH", "CHARSET", "UTF-8", search_term.encode("utf-8"))
            else:
                status, data = m.uid("SEARCH", None, search_term)
            if status != "OK":
                return _err(f"IMAP SEARCH failed: {data!r}")
            uids = (data[0] or b"").split()
            uids = uids[-limit:][::-1]  # newest first, capped
            if not uids:
                return _ok(f"No mail in {mailbox} matching {search_term}.")
            rows: list[str] = []
            for uid in uids:
                row = _fetch_envelope(m, uid)
                if row:
                    rows.append(row)
            return _ok("\n".join(rows))
    except (ICloudNotConfigured, MailNotConfigured) as e:
        return _err(str(e))
    except imaplib.IMAP4.error as e:
        return _err(f"IMAP error: {e}")
    except Exception as e:  # noqa: BLE001
        return _err(f"mail_search failed: {type(e).__name__}: {e}")


def _quote_mailbox(name: str) -> str:
    """Quote any mailbox name. RFC 3501 requires quoting for any non-atom
    char (spaces, brackets, etc.); always-quoting is simpler and safe.
    """
    return f'"{name}"'


def _fetch_envelope(m: imaplib.IMAP4_SSL, uid: bytes) -> str:
    """Return one summary line for a UID, or a `[fetch failed uid=…]` placeholder.

    Honest hit-count beats a silently-shorter result list: a UID that
    SEARCH matched but FETCH can't decode still belongs in the output.
    """
    uid_text = uid.decode("ascii", errors="strict")
    status, data = m.uid(
        "FETCH",
        uid_text,
        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[TEXT]<0.250>)",
    )
    if status != "OK" or not data or not isinstance(data[0], tuple):
        _log.warning("FETCH failed for uid=%s: status=%s", uid, status)
        return f"uid={uid.decode(errors='replace')} | [fetch failed]"
    # Servers answer the two requested sections in their own order: iCloud sends
    # the headers first, Gmail sends the body first. Pick each part by its label.
    headers_raw = b""
    body_preview = b""
    for part in data:
        if not isinstance(part, tuple):
            continue
        if b"HEADER.FIELDS" in part[0].upper():
            headers_raw = part[1] or b""
        elif b"BODY[TEXT]" in part[0].upper():
            body_preview = part[1] or b""
    headers = email.message_from_bytes(headers_raw)
    from_ = _decode_header(headers.get("From")).strip()
    subject = _decode_header(headers.get("Subject") or "(no subject)").strip()
    date_hdr = (headers.get("Date") or "").strip()
    snippet = body_preview.decode(errors="replace").strip().replace("\n", " ")[:120]
    # Every value fetched from the message is untrusted, including Date.
    # Bracket them so an injected imperative reads as data in the summary.
    return (
        f"uid={uid.decode()} | date={_bracket(date_hdr)} | from={_bracket(from_)} "
        f"| subject={_bracket(subject)} | snippet={_bracket(snippet)}"
    )


@brain_tool(
    name="mail_get_message",
    description=(
        "Fetch a single mail message by UID and mailbox. Returns the From, "
        "Subject, Date, To, Cc, Message-ID, In-Reply-To, References, and "
        "the body (text/plain preferred, falling back to text/html stripped)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["icloud", "gmail"], "description": "Mail account. Default icloud."},
            "uid": {"type": "string", "description": "Message UID from mail_search."},
            "mailbox": {"type": "string", "description": "IMAP folder. Default INBOX."},
            "max_body_chars": {"type": "integer", "minimum": 1, "maximum": _MAX_MAIL_BODY_CHARS, "description": f"Truncate body to this many chars. Default 4000, hard cap {_MAX_MAIL_BODY_CHARS}."},
        },
        "required": ["uid"],
    },
)
async def mail_get_message(args):
    provider = (args.get("provider") or "icloud").strip()
    uid_str = (args.get("uid") or "").strip()
    mailbox = (args.get("mailbox") or _DEFAULT_MAILBOX).strip()
    try:
        max_body = _bounded_int(
            args.get("max_body_chars"), default=4000,
            minimum=1, maximum=_MAX_MAIL_BODY_CHARS,
        )
    except ValueError as exc:
        return _err(f"max_body_chars {exc}")
    if not uid_str:
        return _err("uid is required")
    try:
        uid_str = validate_single_uid(uid_str)
        mailbox = validate_mailbox_name(mailbox)
    except (TypeError, ValueError) as exc:
        return _err(str(exc))
    try:
        with _session_for(provider) as m:
            raw = fetch_raw_message(m, uid_str, mailbox)
        msg = email.message_from_bytes(raw)
        return _ok(_format_message(msg, max_body))
    except (TypeError, ValueError) as e:
        return _err(str(e))
    except (ICloudNotConfigured, MailNotConfigured) as e:
        return _err(str(e))
    except (imaplib.IMAP4.error, RuntimeError) as e:
        return _err(f"IMAP error: {e}")
    except Exception as e:  # noqa: BLE001
        return _err(f"mail_get_message failed: {type(e).__name__}: {e}")


def _format_message(msg: email.message.Message, max_body: int) -> str:
    headers = []
    sender = ""
    for h in ("From", "To", "Cc", "Date", "Subject", "Message-ID", "In-Reply-To", "References"):
        v = msg.get(h)
        if not v:
            continue
        # RFC 2047 decode the human-facing fields; keep IDs / Date raw.
        if h in ("From", "To", "Cc", "Subject"):
            v = _decode_header(v)
        # All RFC 5322 header values arrive over an untrusted transport. Even
        # structural fields such as Date and Message-ID can be attacker-set.
        headers.append(f"{h}: {_bracket(v)}")
        if h == "From":
            sender = v
    body = _extract_body(msg)
    if len(body) > max_body:
        body = body[:max_body] + f"\n\n[truncated, {len(body) - max_body} more chars]"
    wrapped_body = _wrap_external(body, source=f"mail/from {sender}" if sender else "mail")
    return "\n".join(headers) + "\n\n" + wrapped_body


def _extract_body(msg: email.message.Message) -> str:
    """Return text/plain body if present, else text/html stripped, else empty."""
    plain: list[str] = []
    html: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            if ctype == "text/plain":
                plain.append(_decode_part(part))
            elif ctype == "text/html":
                html.append(_decode_part(part))
    else:
        payload = _decode_part(msg)
        if msg.get_content_type() == "text/html":
            html.append(payload)
        else:
            plain.append(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        return _strip_html("\n".join(html)).strip()
    return ""


def _decode_part(part: email.message.Message) -> str:
    raw_payload = part.get_payload(decode=True)
    if isinstance(raw_payload, bytes):
        payload = raw_payload
    elif isinstance(raw_payload, str):
        charset = part.get_content_charset() or "utf-8"
        try:
            payload = raw_payload.encode(charset, errors="replace")
        except LookupError:
            payload = raw_payload.encode("utf-8", errors="replace")
    else:
        payload = b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    return _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", s)).strip()


# ---------------------------------------------------------------------------
# Reminders (EventKit)
# ---------------------------------------------------------------------------


@brain_tool(
    name="reminders_list",
    description=(
        "List Apple Reminders entries. By default returns only incomplete "
        "reminders across all lists. Each line shows: list / title / due / "
        "priority / identifier (use that with reminders_get for full detail). "
        "First call may pause briefly while macOS shows the EventKit access "
        "prompt — once granted the daemon doesn't ask again."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "description": "Filter to one Reminders list (case-insensitive). Optional."},
            "include_completed": {"type": "boolean", "description": "Include completed reminders. Default false."},
            "limit": {"type": "integer", "description": f"Cap on returned reminders. Default {_MAX_REMINDERS}."},
        },
    },
)
async def reminders_list(args):
    name_filter = (args.get("list_name") or "").strip().lower()
    include_completed = bool(args.get("include_completed"))
    limit = min(int(args.get("limit") or _MAX_REMINDERS), _MAX_REMINDERS)
    try:
        store = eventkit_store()
    except Exception as e:  # noqa: BLE001 — TCC denial / EventKit failure
        return _err(str(e))

    calendars = store.calendarsForEntityType_(1)  # 1 = EKEntityTypeReminder
    if name_filter:
        calendars = [c for c in calendars if name_filter in str(c.title()).lower()]
    if not calendars:
        return _ok("No reminder lists matched.")

    predicate = (
        store.predicateForRemindersInCalendars_(calendars)
        if include_completed
        else store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
            None, None, calendars
        )
    )

    import threading
    done = threading.Event()
    bucket: dict[str, Any] = {"items": []}

    def cb(items):
        bucket["items"] = list(items or [])
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(predicate, cb)
    if not done.wait(timeout=10.0):
        _log.warning("EventKit reminder fetch timed out after 10s")
        return _err("EventKit fetch timed out")

    rows: list[str] = []
    for r in bucket["items"][:limit]:
        rows.append(_format_reminder_line(r))
    if not rows:
        return _ok("No reminders found.")
    return _ok("\n".join(rows))


def _reminder_due_local(r) -> datetime | None:
    """A reminder's due time in local time, or None when it has none."""
    components = r.dueDateComponents()
    due_date = components.date() if components is not None else None
    if due_date is None:
        return None
    return datetime.fromtimestamp(float(due_date.timeIntervalSince1970()), tz=UTC).astimezone()


def _format_reminder_line(r) -> str:
    title = str(r.title() or "(untitled)")
    list_name = str(r.calendar().title())
    local_due = _reminder_due_local(r)
    due = local_due.strftime("%Y-%m-%d %H:%M (local time)") if local_due else "—"
    priority = int(r.priority() or 0)
    ident = str(r.calendarItemIdentifier())
    completed = "✓" if r.isCompleted() else "•"
    return (
        f"{completed} {_bracket(list_name)} | title={_bracket(title)} "
        f"| due={due} | alarms={len(list(r.alarms() or []))} "
        f"| p={priority} | id={_bracket(ident)}"
    )


@brain_tool(
    name="reminders_get",
    description="Fetch one reminder by its calendarItemIdentifier (from reminders_list).",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
        },
        "required": ["id"],
    },
)
async def reminders_get(args):
    ident = (args.get("id") or "").strip()
    if not ident:
        return _err("id is required")
    try:
        store = eventkit_store()
    except Exception as e:  # noqa: BLE001
        return _err(str(e))
    item = store.calendarItemWithIdentifier_(ident)
    if item is None:
        return _err(f"reminder {ident!r} not found")
    title = str(item.title() or "(untitled)")
    list_name = str(item.calendar().title())
    parts = [
        f"title: {_bracket(title)}",
        f"list: {_bracket(list_name)}",
        f"completed: {bool(item.isCompleted())}",
        f"priority: {int(item.priority() or 0)}",
    ]
    if item.dueDateComponents() is not None and item.dueDateComponents().date():
        parts.append(f"due: {item.dueDateComponents().date()}")
    if item.notes():
        # Reminders can be created via shared lists or Siri-from-contact;
        # treat the note as sender-controlled for consistency with mail
        # and contact notes.
        parts.append(
            f"notes:\n{_wrap_external(str(item.notes()), source='reminder/notes')}"
        )
    alarms = list(item.alarms() or [])
    parts.append(f"alarm_count: {len(alarms)}")
    for alarm in alarms:
        absolute = alarm.absoluteDate()
        relative = alarm.relativeOffset()
        if absolute is not None:
            parts.append(f"alarm_absolute: {_bracket(absolute)}")
        elif relative is not None:
            parts.append(f"alarm_relative_seconds: {_bracket(relative)}")
    parts.append(f"id: {_bracket(ident)}")
    return _ok("\n".join(parts))


# ---------------------------------------------------------------------------
# Contacts (CardDAV)
# ---------------------------------------------------------------------------


# iCloud returns address-data ONLY via a REPORT addressbook-query — a PROPFIND
# returns the hrefs+etags with an empty address-data element, so every card
# would parse to empty. The empty <c:filter/> matches all cards in the book.
_CONTACTS_LIST_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    '<d:prop>'
    '<d:getetag/>'
    '<c:address-data/>'
    '</d:prop>'
    '<c:filter/>'
    '</c:addressbook-query>'
)


def _list_cards(client, ab_url: str):
    """Return list of (href, etag, vcard_text) for every card in the book."""
    import defusedxml.ElementTree as ET  # XXE-safe drop-in for server-supplied XML

    resp = client.request(
        "REPORT",
        ab_url,
        content=_CONTACTS_LIST_BODY,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    out: list[tuple[str, str, str]] = []
    for response in root.findall("d:response", CARDDAV_NS):
        href_el = response.find("d:href", CARDDAV_NS)
        if href_el is None or not (href_el.text or "").strip():
            continue
        href = href_el.text.strip()
        # Skip the addressbook root itself; only individual cards.
        if href.rstrip("/") == ab_url.rstrip("/"):
            continue
        etag = ""
        et_el = response.find(".//d:getetag", CARDDAV_NS)
        if et_el is not None:
            etag = (et_el.text or "").strip().strip('"')
        addr_el = response.find(".//c:address-data", CARDDAV_NS)
        vcard = (addr_el.text or "") if addr_el is not None else ""
        if vcard:
            out.append((href, etag, vcard))
    return out


_VCARD_FULL_DATE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")
_VCARD_YEARLESS_DATE = re.compile(r"^--(\d{2})-?(\d{2})$")
# Apple stores "no year known" birthdays/anniversaries with the sentinel
# year 1604 — collapse those to a year-less '--MM-DD'.
_APPLE_NO_YEAR = {"1604"}


def _norm_vcard_date(raw: str | None) -> str | None:
    """Normalize a vCard date value to 'YYYY-MM-DD' or year-less '--MM-DD'.

    Handles year-less birthdays ('--03-15'), compact forms ('19900315'), and
    Apple's 1604 no-year sentinel. Returns None for anything unparseable.
    """
    if not raw:
        return None
    s = str(raw).strip()
    m = _VCARD_YEARLESS_DATE.match(s)
    if m:
        return f"--{m.group(1)}-{m.group(2)}"
    m = _VCARD_FULL_DATE.match(s)
    if m:
        year, month, day = m.group(1), m.group(2), m.group(3)
        if year in _APPLE_NO_YEAR:
            return f"--{month}-{day}"
        return f"{year}-{month}-{day}"
    return None


def _extract_anniversary(v) -> str | None:
    """Pull an anniversary date from a parsed vCard.

    Prefers the vCard 4.0 ANNIVERSARY property; falls back to Apple's grouped
    'itemN.X-ABDATE' correlated with the sibling 'itemN.X-ABLabel' whose value
    contains 'Anniversary' (e.g. '_$!<Anniversary>!$_').
    """
    if hasattr(v, "anniversary"):
        norm = _norm_vcard_date(getattr(v.anniversary, "value", None))
        if norm:
            return norm
    labels: dict[str, str] = {}
    dates: dict[str, str] = {}
    for child in v.getChildren():
        group = getattr(child, "group", None)
        if not group:
            continue
        name = (child.name or "").lower()
        if name == "x-ablabel":
            labels[group] = str(child.value or "")
        elif name == "x-abdate":
            dates[group] = str(child.value or "")
    for group, dval in dates.items():
        if "anniversary" in labels.get(group, "").lower():
            norm = _norm_vcard_date(dval)
            if norm:
                return norm
    return None


def _vcard_summary(vcard_text: str, href: str) -> dict[str, Any]:
    """Parse one vCard into a flat dict for matching + display."""
    import vobject

    try:
        v = vobject.readOne(vcard_text)
    except Exception as e:  # noqa: BLE001
        _log.warning("vCard parse failed for %s: %s", href, type(e).__name__)
        return {"href": href, "fn": "(unparseable)"}
    out: dict[str, Any] = {"href": href}
    if hasattr(v, "fn"):
        out["fn"] = v.fn.value
    if hasattr(v, "n") and v.n.value:
        n = v.n.value
        out["family"] = getattr(n, "family", "") or ""
        out["given"] = getattr(n, "given", "") or ""
    out["emails"] = [e.value for e in getattr(v, "email_list", [])]
    out["tels"] = [t.value for t in getattr(v, "tel_list", [])]
    out["org"] = (
        v.org.value[0] if hasattr(v, "org") and isinstance(v.org.value, list) and v.org.value
        else (v.org.value if hasattr(v, "org") else "")
    )
    if hasattr(v, "uid"):
        out["uid"] = v.uid.value
    if hasattr(v, "note"):
        out["note"] = v.note.value
    # BDAY + anniversary — needed by the occasions seeder. (Historically the
    # parse loop ignored these; nothing else in the codebase read them.)
    bday = _norm_vcard_date(v.bday.value) if hasattr(v, "bday") else None
    if bday:
        out["bday"] = bday
    anniversary = _extract_anniversary(v)
    if anniversary:
        out["anniversary"] = anniversary
    return out


def _matches(card: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    q = query.lower()
    fields = [
        card.get("fn", ""), card.get("family", ""), card.get("given", ""),
        card.get("org", ""), card.get("note", ""),
    ]
    fields.extend(card.get("emails") or [])
    fields.extend(card.get("tels") or [])
    return any(q in str(f).lower() for f in fields if f)


@brain_tool(
    name="contacts_search",
    description=(
        "Search iCloud contacts by free-text query (matches name, org, email, "
        "phone, note). Returns one line per hit: name | emails | phones | org "
        "| href (pass href to contacts_get for full detail). Default limit 25. "
        "First call discovers the user's address-book URL, then it's cached "
        "for the process lifetime."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-text. Matches any contact field."},
            "limit": {"type": "integer", "description": "Max hits. Default 25."},
        },
        "required": ["query"],
    },
)
async def contacts_search(args):
    query = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 25)
    if not query:
        return _err("query is required")
    try:
        with carddav_http_client() as client:
            ab = carddav_addressbook_url(client)
            cards_raw = _list_cards(client, ab)
    except ICloudNotConfigured as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"CardDAV list failed: {type(e).__name__}: {e}")

    hits: list[dict[str, Any]] = []
    for href, _etag, vcard in cards_raw:
        card = _vcard_summary(vcard, href)
        if _matches(card, query):
            hits.append(card)
            if len(hits) >= limit:
                break

    if not hits:
        return _ok(f"No contacts matched {query!r}.")
    rows = []
    for c in hits:
        name = c.get("fn") or f"{c.get('given', '')} {c.get('family', '')}".strip() or "(no name)"
        emails = ",".join(c.get("emails") or []) or "—"
        tels = ",".join(c.get("tels") or []) or "—"
        org = c.get("org") or "—"
        # Every visible vCard field is sender-controlled (the contact card
        # itself, or whoever exported it to the user). Bracket-mark.
        rows.append(" | ".join([
            f"name={_bracket(name)}",
            f"emails={_bracket(emails)}",
            f"tels={_bracket(tels)}",
            f"org={_bracket(org)}",
            f"href={_bracket(c['href'])}",
        ]))
    return _ok("\n".join(rows))


@brain_tool(
    name="contacts_get",
    description=(
        "Fetch one contact's full vCard by href (from contacts_search). "
        "Returns name, all email addresses (with type), all phone numbers "
        "(with type), org, title, addresses, notes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "href": {"type": "string"},
        },
        "required": ["href"],
    },
)
async def contacts_get(args):
    import vobject

    from src.brain.mcp.tools._icloud import ICLOUD_CARDDAV_URL, _join

    href = (args.get("href") or "").strip()
    if not href:
        return _err("href is required")
    try:
        with carddav_http_client() as client:
            url = assert_icloud_host(_join(ICLOUD_CARDDAV_URL, href))
            resp = client.get(url, headers={"Accept": "text/vcard"})
            resp.raise_for_status()
            vcard_text = resp.text
    except ICloudNotConfigured as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"CardDAV GET failed: {type(e).__name__}: {e}")

    try:
        v = vobject.readOne(vcard_text)
    except Exception as e:  # noqa: BLE001
        return _err(f"vCard parse failed: {e}")

    parts: list[str] = []
    # Every vCard property below is sender-controlled (contact owner can
    # set FN/N/ORG/TITLE/ADR to anything). Bracket-mark short values; the
    # NOTE field is wrapped via <external_content> further down.
    if hasattr(v, "fn"):
        parts.append(f"name: {_bracket(v.fn.value)}")
    if hasattr(v, "n") and v.n.value:
        n = v.n.value
        parts.append(f"family: {_bracket(getattr(n, 'family', '') or '—')}")
        parts.append(f"given: {_bracket(getattr(n, 'given', '') or '—')}")
    for email_prop in getattr(v, "email_list", []):
        types = ",".join(email_prop.params.get("TYPE", [])) or "—"
        parts.append(f"email ({_bracket(types)}): {_bracket(email_prop.value)}")
    for t in getattr(v, "tel_list", []):
        types = ",".join(t.params.get("TYPE", [])) or "—"
        parts.append(f"tel ({_bracket(types)}): {_bracket(t.value)}")
    if hasattr(v, "org"):
        org = v.org.value
        org_str = org[0] if isinstance(org, list) and org else org
        parts.append(f"org: {_bracket(org_str)}")
    if hasattr(v, "title"):
        parts.append(f"title: {_bracket(v.title.value)}")
    for adr in getattr(v, "adr_list", []):
        types = ",".join(adr.params.get("TYPE", [])) or "—"
        a = adr.value
        addr_str = ", ".join(filter(None, [
            getattr(a, "street", ""), getattr(a, "city", ""),
            getattr(a, "region", ""), getattr(a, "code", ""),
            getattr(a, "country", ""),
        ]))
        parts.append(f"address ({_bracket(types)}): {_bracket(addr_str)}")
    if hasattr(v, "note"):
        fn_prop = getattr(v, "fn", None)
        fn = str(fn_prop.value) if fn_prop is not None else "unknown"
        parts.append(f"note:\n{_wrap_external(v.note.value, source=f'contact/{fn}')}")
    if hasattr(v, "uid"):
        parts.append(f"uid: {_bracket(v.uid.value)}")
    parts.append(f"href: {_bracket(href)}")
    return _ok("\n".join(parts))


def _fetch_reminders(store, predicate) -> list:
    import threading

    done = threading.Event()
    bucket: dict[str, Any] = {"items": []}

    def cb(items):
        bucket["items"] = list(items or [])
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(predicate, cb)
    if not done.wait(timeout=10.0):
        raise TimeoutError("EventKit fetch timed out")
    return bucket["items"]


def today_events(day: date) -> list[tuple[str, str]]:
    """Calendar events that start on `day`, as (local time or "all day", title). Raises on failure."""
    calendars = caldav_client().principal().calendars()
    start_dt, end_dt = _local_day_window(day, day + timedelta(days=1))
    found: list[tuple[str, str]] = []
    for cal in calendars:
        for ev in cal.search(start=start_dt, end=end_dt, event=True, expand=True):
            summary, start_str, _end = _event_summary_times(ev)
            try:
                start = datetime.fromisoformat(start_str)
            except ValueError:
                try:
                    start_day = date.fromisoformat(start_str[:10])
                except ValueError:
                    continue
                if start_day == day:
                    found.append(("all day", summary))
                continue
            local = start.astimezone() if start.tzinfo else start
            if local.date() == day:
                found.append((local.strftime("%H:%M"), summary))
    return sorted(set(found))


def reminders_due_on(day: date) -> list[tuple[str, str]]:
    """Open reminders due on `day`, as (local time, title). Raises on failure."""
    store = eventkit_store()
    calendars = store.calendarsForEntityType_(1)
    predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, calendars)
    found = []
    for r in _fetch_reminders(store, predicate):
        local = _reminder_due_local(r)
        if local is None or local.date() != day:
            continue
        hour = r.dueDateComponents().hour()
        timed = isinstance(hour, int) and 0 <= hour <= 23
        found.append((local.strftime("%H:%M") if timed else "all day", str(r.title() or "(untitled)")))
    return sorted(found)
