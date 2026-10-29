from src.db import LATEST_SCHEMA_VERSION, get_card, record_card, set_card_status


def test_migration_19_stores_cards(isolated_db):
    assert LATEST_SCHEMA_VERSION >= 19
    card_id = record_card(
        place="private", channel="DPRIV", summary_ts="1.1", detail_ts="1.2", area="humans",
        follow_up_id="F-000001", card={"title": "Anjali", "approvals": [{"id": "abcdef123456", "label": "Send"}]},
    )
    card = get_card(card_id)
    assert (card["channel"], card["summary_ts"], card["detail_ts"], card["status"]) == ("DPRIV", "1.1", "1.2", "open")
    assert card["card"]["approvals"][0]["label"] == "Send" and card["follow_up_id"] == "F-000001"


def test_card_status_changes_and_unknown_is_none(isolated_db):
    card_id = record_card(place="shared", channel="CS", summary_ts="2.1", detail_ts="2.2", area="family",
                          follow_up_id=None, card={"title": "x"})
    assert set_card_status(card_id, "done")
    assert get_card(card_id)["status"] == "done"
    assert get_card(card_id + 99) is None
