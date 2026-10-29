from __future__ import annotations


def test_dedup_window_purges_old():
    from src.orchestrator import reply_routing as rr

    rr._recent_events.clear()
    rr._recent_events["old"] = 0.0
    rr._seen_recently("new")
    assert "old" not in rr._recent_events
