from __future__ import annotations

from src.brain.mcp.tools.icloud_read import _norm_vcard_date, _vcard_summary


def _summary(vcard: str) -> dict:
    return _vcard_summary(vcard, href="/x.vcf")


def test_standard_bday():
    card = _summary("BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nBDAY:1990-03-15\nEND:VCARD\n")
    assert card["bday"] == "1990-03-15"


def test_yearless_bday():
    card = _summary("BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nBDAY:--03-15\nEND:VCARD\n")
    assert card["bday"] == "--03-15"


def test_apple_1604_sentinel_collapses_to_yearless():
    card = _summary("BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nBDAY:1604-03-15\nEND:VCARD\n")
    assert card["bday"] == "--03-15"


def test_norm_compact_and_garbage():
    assert _norm_vcard_date("19900315") == "1990-03-15"
    assert _norm_vcard_date("--0315") == "--03-15"
    assert _norm_vcard_date("garbage") is None
    assert _norm_vcard_date("") is None
    assert _norm_vcard_date(None) is None


def test_apple_grouped_anniversary():
    vcard = (
        "BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\n"
        "item1.X-ABDATE;type=pref:2015-06-12\n"
        "item1.X-ABLabel:_$!<Anniversary>!$_\n"
        "END:VCARD\n"
    )
    assert _summary(vcard)["anniversary"] == "2015-06-12"


def test_no_dates_means_absent_keys():
    card = _summary("BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nEND:VCARD\n")
    assert "bday" not in card
    assert "anniversary" not in card
