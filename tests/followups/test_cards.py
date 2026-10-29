import json

import pytest

from src.brain.followups.cards import (
    CARD_ACTIONS,
    detail_blocks,
    parse_card,
    parse_overview,
    summary_blocks,
    summary_text,
)

ANJALI = {
    "area": "humans", "emoji": "🎂", "title": "Anjali Rao", "line": "Anjali Rao turns 40 on Wednesday. I have no notes on her yet. Tap Draft wishes and I'll write them.",
    "detail": {
        "heading": "Anjali Rao · Wed 21 Oct",
        "quote": "Her page is empty. Reply with a line about her and I'll draft the wishes.",
        "bullets": ["**Alex** · Thu 22", "**Sam** · Sat 24"],
        "context": "From the people diary",
    },
    "follow_up_id": "F-000001",
    "asks": ["Draft wishes"],
}


def ids(blocks):
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def labels(blocks):
    return [e["text"]["text"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_the_summary_is_emoji_bold_title_then_the_sentences_below():
    card = parse_card(ANJALI)
    assert summary_text(card) == "🎂 *Anjali Rao*\nAnjali Rao turns 40 on Wednesday. I have no notes on her yet. Tap Draft wishes and I'll write them."
    blocks = summary_blocks(card, status="open")
    assert len(blocks) == 1 and blocks[0]["text"]["text"] == summary_text(card)


def test_a_finished_card_shows_its_status_under_the_summary():
    card = parse_card(ANJALI)
    for status, word in (("done", "✅ Done"), ("later", "⏰ Tomorrow"), ("dropped", "✖️ Dropped")):
        blocks = summary_blocks(card, status=status)
        assert blocks[-1]["type"] == "context" and word in json.dumps(blocks[-1], ensure_ascii=False)


def test_the_detail_has_heading_quote_bullets_context_then_buttons():
    blocks = detail_blocks(parse_card(ANJALI), card_id=7, status="open")
    text = json.dumps(blocks, ensure_ascii=False)
    assert "*Anjali Rao · Wed 21 Oct*" in text
    assert "> Her page is empty." in text
    assert "• *Alex* · Thu 22" in text
    assert [b["type"] for b in blocks][-2:] == ["context", "actions"]
    assert ids(blocks) == ["card_ask", "card_done", "card_later", "card_drop"]
    assert labels(blocks) == ["Draft wishes", "✅ Done", "⏰ Tomorrow", "✖️ Drop"]
    first = blocks[-1]["elements"][0]
    assert first.get("style") == "primary" and first["value"] == "7:0"
    assert blocks[-1]["elements"][1]["value"] == "7"


def test_a_card_without_a_follow_up_offers_done_only():
    card = parse_card({**ANJALI, "follow_up_id": None, "asks": []})
    blocks = detail_blocks(card, card_id=3, status="open")
    assert ids(blocks) == ["card_done"] and blocks[-1]["elements"][0].get("style") == "primary"


def test_approvals_come_first_and_an_approved_one_becomes_a_line():
    card = parse_card({**ANJALI, "approvals": [{"id": "abcdef123456", "label": "Send wishes"}], "asks": []})
    blocks = detail_blocks(card, card_id=4, status="open")
    assert ids(blocks)[0] == "card_approve" and labels(blocks)[0] == "Send wishes"
    after = detail_blocks(card, card_id=4, status="open", approved={"abcdef123456"})
    assert "card_approve" not in ids(after) and "Approved: Send wishes" in json.dumps(after)


def test_a_finished_card_has_no_buttons():
    blocks = detail_blocks(parse_card(ANJALI), card_id=7, status="done")
    assert not ids(blocks) and "✅ Done" in json.dumps(blocks, ensure_ascii=False)


def test_text_is_cleaned_and_mentions_are_neutralised():
    card = parse_card({**ANJALI, "line": ANJALI["line"] + " hi <!channel> **all**"})
    assert "<!channel>" not in summary_text(card)


def test_a_card_takes_three_full_sentences_and_the_overview_stays_one_line():
    three = (
        "Two library books are due on Friday and both have a waiting list, so they cannot be renewed. "
        "The branch closes at 18:00. Take them with you this morning."
    )
    assert parse_card({**ANJALI, "line": three}).line == three
    with pytest.raises(ValueError, match="overview line"):
        parse_overview({"line": "x" * 201})


def test_the_overview_has_a_line_and_detail_and_no_buttons():
    overview = parse_overview({"line": "Saturday, 17 Oct. 4 things need you.", "detail": {"bullets": ["11:00 Music class"]}})
    assert summary_text(overview).startswith("☀️ *")
    assert not ids(detail_blocks(overview, card_id=1, status="open"))


@pytest.mark.parametrize("bad, field", [
    ({**ANJALI, "title": ""}, "title"),
    ({**ANJALI, "line": "x" * 401}, "line"),
    ({**ANJALI, "line": "turns 40 on Wednesday"}, "two or three plain sentences"),
    ({**ANJALI, "asks": ["a", "b", "c"]}, "asks"),
    ({**ANJALI, "asks": ["x" * 41]}, "asks"),
    ({**ANJALI, "approvals": [{"id": "nope", "label": "Send"}]}, "approvals"),
    ({**ANJALI, "follow_up_id": "12"}, "follow_up_id"),
    ({**ANJALI, "detail": {"bullets": ["b"] * 9}}, "bullets"),
    ({**ANJALI, "emoji": "x" * 20}, "emoji"),
])
def test_bad_cards_are_refused_with_the_field_named(bad, field):
    with pytest.raises(ValueError, match=field):
        parse_card(bad)


def test_every_button_id_is_known():
    card = parse_card({**ANJALI, "approvals": [{"id": "abcdef123456", "label": "Send"}]})
    assert set(ids(detail_blocks(card, card_id=1, status="open"))) <= set(CARD_ACTIONS)
