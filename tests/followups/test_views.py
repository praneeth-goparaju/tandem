import json

from src.brain.followups.views import (
    ACTION_IDS,
    HomeData,
    details_modal,
    done_modal,
    home_view,
    stranger_home_view,
)


def item(n: int, *, outcome: str = "asked", action_ids=(), summary: str | None = None) -> dict:
    return {
        "id": f"F-{n:06d}", "summary": summary or f"Loop number {n}", "status": "raised",
        "outcome": outcome, "research": f"Note for {n}\nsecond line", "action_ids": list(action_ids),
    }


def proposal(action_id: str, kind: str = "send_draft") -> dict:
    return {"id": action_id, "type": kind, "rationale": f"Reply about {action_id}", "status": "pending"}


def all_action_ids(blocks) -> set[str]:
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "action_id" in node:
                found.add(node["action_id"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blocks)
    return found


def test_every_home_button_carries_an_id_the_listener_routes():
    ids = all_action_ids(home_view(_home("F1")))
    assert ids == set(ACTION_IDS) == {"fu_details", "digest_show_done"}


def test_item_text_is_escaped():
    dumped = json.dumps(home_view(HomeData(needs_you=[item(1, summary="<!channel> & co")])))
    assert "&lt;!channel&gt; &amp; co" in dumped and "<!channel>" not in dumped


def test_modals_list_their_content():
    details = details_modal(item(1, outcome="prepared"), [proposal("abcdef123456")])
    assert details["type"] == "modal" and "Note for 1" in json.dumps(details) and "abcdef123456" in json.dumps(details)
    done = done_modal([item(1, outcome="done"), item(2, outcome="done")])
    assert "Loop number 1" in json.dumps(done) and "Loop number 2" in json.dumps(done)


def _home(file_id):
    return HomeData(
        needs_you=[item(1)], today=["09:00 Dentist"], health_line="Weight 82.1 kg",
        weight_file_id=file_id, open_loops=[item(2)], open_count=4, handled=[item(3, outcome="done")],
    )


def test_home_view_has_the_five_sections_and_image_only_with_a_file():
    view = home_view(_home("F123"))
    assert view["type"] == "home"
    headers = [b["text"]["text"] for b in view["blocks"] if b["type"] == "header"]
    assert headers == ["Needs you", "Today", "Health", "Open loops", "Done overnight"]
    images = [b for b in view["blocks"] if b["type"] == "image"]
    assert images and images[0]["slack_file"] == {"id": "F123"}
    assert not [b for b in home_view(_home(None))["blocks"] if b["type"] == "image"]


def test_stranger_home_view_is_one_line():
    view = stranger_home_view()
    assert view["type"] == "home" and len(view["blocks"]) == 1
    assert "This is a private assistant." in json.dumps(view)


def test_long_text_is_cut_to_slack_limits():
    view = home_view(HomeData(needs_you=[item(1, summary="x" * 5000)]))
    for block in view["blocks"]:
        assert len((block.get("text") or {}).get("text", "")) <= 3000


def test_the_largest_home_view_fits_slacks_block_limit():
    items = [item(n, outcome="prepared", action_ids=[f"{n:012x}"]) for n in range(1, 30)]
    home = home_view(HomeData(needs_you=items, today=["x"] * 40, health_line="h", weight_file_id="F1",
                              open_loops=items, open_count=29, handled=items))
    assert len(home["blocks"]) <= 100
