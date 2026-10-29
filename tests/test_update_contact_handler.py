from __future__ import annotations

import vobject

from src.brain.actions.handlers.update_contact import _patch_vcard

_BASE_VCARD = """BEGIN:VCARD
VERSION:3.0
UID:UID-X
FN:Jane Doe
N:Doe;Jane;;;
EMAIL:jane@example.com
TEL:+10000000000
NOTE:initial
END:VCARD
"""


def _load() -> vobject.base.Component:
    return vobject.readOne(_BASE_VCARD)


def test_set_emails_replaces_existing() -> None:
    v = _load()
    _patch_vcard(v, {"set_emails": ["new1@example.org", "new2@example.org"]})
    emails = [e.value for e in v.email_list]
    assert emails == ["new1@example.org", "new2@example.org"]


def test_add_emails_appends_only() -> None:
    v = _load()
    _patch_vcard(v, {"add_emails": ["extra@example.org"]})
    emails = [e.value for e in v.email_list]
    assert emails == ["jane@example.com", "extra@example.org"]


def test_set_tels_replaces_existing() -> None:
    v = _load()
    _patch_vcard(v, {"set_tels": ["+12222222222"]})
    tels = [t.value for t in v.tel_list]
    assert tels == ["+12222222222"]


def test_note_append_joins_with_newline() -> None:
    v = _load()
    _patch_vcard(v, {"note_append": "second"})
    assert v.note.value == "initial\nsecond"


def test_note_replace_overwrites_existing() -> None:
    v = _load()
    _patch_vcard(v, {"note": "rewritten"})
    assert v.note.value == "rewritten"


def test_full_name_replace() -> None:
    v = _load()
    _patch_vcard(v, {"full_name": "Jane Smith"})
    assert v.fn.value == "Jane Smith"


def test_org_added_when_missing() -> None:
    v = _load()
    _patch_vcard(v, {"org": "Acme"})
    org_val = v.org.value
    org_str = org_val[0] if isinstance(org_val, list) else org_val
    assert org_str == "Acme"
