from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime


def _seed(dbmod, rows):
    conn = dbmod.get_db()
    for r in rows:
        conn.execute(
            "INSERT INTO garmin_days (date, steps, resting_hr, sleep_seconds,"
            " sleep_score, deep_sleep_seconds, light_sleep_seconds,"
            " rem_sleep_seconds, awake_sleep_seconds, nap_seconds,"
            " nap_start_local, nap_end_local, nap_feedback,"
            " hrv_last_night_avg, hrv_status, stress_avg,"
            " body_battery_charged, body_battery_drained, active_kilocalories,"
            " total_kilocalories, activities, synced_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["date"], r.get("steps"), r.get("resting_hr"),
             r.get("sleep_seconds"), r.get("sleep_score"),
             r.get("deep_sleep_seconds"), r.get("light_sleep_seconds"),
             r.get("rem_sleep_seconds"), r.get("awake_sleep_seconds"),
             r.get("nap_seconds"), r.get("nap_start_local"),
             r.get("nap_end_local"), r.get("nap_feedback"),
             r.get("hrv_last_night_avg"), r.get("hrv_status"),
             r.get("stress_avg"), r.get("body_battery_charged"),
             r.get("body_battery_drained"), r.get("active_kilocalories"),
             r.get("total_kilocalories"), r.get("activities", "[]"),
             "2026-08-20T05:00:00"))
    conn.commit()


def _stamp_sync(dbmod, value="2026-08-29T08:00:00"):
    conn = dbmod.get_db()
    conn.execute(
        "INSERT INTO daemon_state(key, value) VALUES('last_garmin_sync', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (value,),
    )
    conn.commit()


def _call(name, args):
    from src.brain.mcp import tool_registry

    tool = next(t for t in tool_registry.all_tools() if t.name == name)
    result = asyncio.run(tool.handler(args))
    return json.loads(result["content"][0]["text"])


def _import_tools():
    import src.brain.mcp.tools.garmin  # noqa: F401


def test_garmin_daily_returns_latest_day_by_default(isolated_db):
    _import_tools()
    _seed(isolated_db, [
        {"date": "2026-08-17", "steps": 2000},
        {"date": "2026-08-18", "steps": 1459, "resting_hr": 78},
    ])
    out = _call("garmin_daily", {})
    assert out["date"] == "2026-08-18"
    assert out["steps"] == 1459
    assert out["resting_hr"] == 78


def test_garmin_daily_specific_date_and_no_data(isolated_db):
    _import_tools()
    _seed(isolated_db, [{"date": "2026-08-18", "steps": 1459}])
    assert _call("garmin_daily", {"date": "2026-08-18"})["steps"] == 1459
    out = _call("garmin_daily", {"date": "2026-08-19"})
    assert out.get("no_data") is True


def test_garmin_daily_returns_nap_and_sleep_stage_detail(isolated_db):
    _import_tools()
    _seed(isolated_db, [{
        "date": "2026-08-23", "steps": 1713,
        "deep_sleep_seconds": 5400, "light_sleep_seconds": 10800,
        "rem_sleep_seconds": 3600, "awake_sleep_seconds": 600,
        "nap_seconds": 4860,
        "nap_start_local": "2026-08-23T15:11:56",
        "nap_end_local": "2026-08-23T16:32:56",
        "nap_feedback": "LONG_DURATION_LOW_NEED",
    }])
    out = _call("garmin_daily", {"date": "2026-08-23"})
    assert out["deep_sleep_seconds"] == 5400
    assert out["light_sleep_seconds"] == 10800
    assert out["rem_sleep_seconds"] == 3600
    assert out["awake_sleep_seconds"] == 600
    assert out["nap_seconds"] == 4860
    assert out["nap_start_local"] == "2026-08-23T15:11:56"
    assert out["nap_end_local"] == "2026-08-23T16:32:56"
    assert out["nap_feedback"] == "LONG_DURATION_LOW_NEED"


def test_garmin_daily_hides_raw_details_unless_requested(isolated_db):
    conn = isolated_db.get_db()
    _import_tools()
    _seed(isolated_db, [{"date": "2026-08-23", "steps": 1713}])
    conn.execute(
        "UPDATE garmin_days SET health_details=? WHERE date=?",
        (json.dumps({"heart_rates": {"heartRateValues": [[1, 70]]}}),
         "2026-08-23"),
    )
    conn.commit()

    compact = _call("garmin_daily", {"date": "2026-08-23"})
    assert "health_details" not in compact
    assert compact["detail_groups_available"] == ["heart_rates"]
    detailed = _call(
        "garmin_daily", {"date": "2026-08-23", "include_details": True}
    )
    assert detailed["health_details"]["heart_rates"]["heartRateValues"] == [[1, 70]]


def test_garmin_trends_aggregates_and_flags_missing(isolated_db):
    _import_tools()
    _seed(isolated_db, [
        {"date": "2026-08-16", "steps": 8949, "resting_hr": 84},
        {"date": "2026-08-18", "steps": 1459, "resting_hr": 78,
         "sleep_seconds": 6 * 3600, "sleep_score": 70},
    ])
    out = _call("garmin_trends", {"days": 14})
    assert out["days_with_data"] == 2
    assert out["steps"]["avg"] == 5204
    assert out["resting_hr"]["avg"] == 81
    assert out["days_with_no_record"] == 12
    assert out["sleep_score"]["days_missing"] == 13
    assert out["hrv_last_night_avg"]["days_missing"] == 14
    assert out["steps"]["days_missing"] == 12


def test_garmin_trends_splits_resting_heart_rate_into_overnight_and_daytime(isolated_db):
    _import_tools()
    _seed(isolated_db, [
        {"date": "2026-08-15", "resting_hr": 80},
        {"date": "2026-08-16", "resting_hr": 76},
        {"date": "2026-08-17", "resting_hr": 70, "sleep_score": 81},
        {"date": "2026-08-18", "resting_hr": 71, "sleep_score": 75},
        {"date": "2026-08-19", "steps": 4000},
    ])
    split = _call("garmin_trends", {"days": 14})["resting_hr_by_kind"]
    assert split["overnight"] == {"days": 2, "avg": 70.5, "min": 70, "max": 71}
    assert split["daytime"] == {"days": 2, "avg": 78.0, "min": 76, "max": 80}


def test_garmin_trends_empty_db_is_explicit(isolated_db):
    _import_tools()
    out = _call("garmin_trends", {})
    assert out["days_with_data"] == 0


def test_garmin_daily_reports_fresh_pipeline(monkeypatch, isolated_db):
    _import_tools()
    _seed(isolated_db, [{"date": "2026-08-29", "steps": 5000}])
    _stamp_sync(isolated_db)
    from src.brain.mcp.tools import garmin
    from src.orchestrator import config

    monkeypatch.setattr(config, "GARMIN_ENABLED", True)
    monkeypatch.setattr(garmin, "_now_utc", lambda: datetime(
        2026, 8, 29, 10, tzinfo=UTC
    ))
    out = _call("garmin_daily", {})

    assert out["freshness"]["stale"] is False
    assert out["freshness"]["latest_data_age_days"] == 0
    assert out["freshness"]["pipeline_last_success_age_hours"] == 2.0


def test_garmin_trends_reports_stale_pipeline(monkeypatch, isolated_db):
    _import_tools()
    _seed(isolated_db, [{"date": "2026-08-27", "steps": 5000}])
    _stamp_sync(isolated_db, "2026-08-23T08:00:00")
    from src.brain.mcp.tools import garmin
    from src.orchestrator import config

    monkeypatch.setattr(config, "GARMIN_ENABLED", True)
    monkeypatch.setattr(garmin, "_now_utc", lambda: datetime(
        2026, 8, 29, 10, tzinfo=UTC
    ))
    out = _call("garmin_trends", {"days": 7})

    assert out["freshness"]["stale"] is True
    assert any(
        "successful Garmin sync" in reason
        for reason in out["freshness"]["reasons"]
    )


def test_garmin_specific_historical_day_still_reports_pipeline_freshness(
    monkeypatch, isolated_db,
):
    _import_tools()
    _seed(isolated_db, [
        {"date": "2026-08-10", "steps": 4000},
        {"date": "2026-08-29", "steps": 5000},
    ])
    _stamp_sync(isolated_db)
    from src.brain.mcp.tools import garmin
    from src.orchestrator import config

    monkeypatch.setattr(config, "GARMIN_ENABLED", True)
    monkeypatch.setattr(garmin, "_now_utc", lambda: datetime(
        2026, 8, 29, 10, tzinfo=UTC
    ))
    out = _call("garmin_daily", {"date": "2026-08-10"})

    assert out["date"] == "2026-08-10"
    assert out["freshness"]["latest_data_date"] == "2026-08-29"
    assert out["freshness"]["stale"] is False


def test_garmin_freshness_explains_disabled_scheduler(
    monkeypatch, isolated_db,
):
    _import_tools()
    _seed(isolated_db, [{"date": "2026-08-29", "steps": 5000}])
    _stamp_sync(isolated_db)
    from src.brain.mcp.tools import garmin
    from src.orchestrator import config

    monkeypatch.setattr(config, "GARMIN_ENABLED", False)
    monkeypatch.setattr(garmin, "_now_utc", lambda: datetime(
        2026, 8, 29, 10, tzinfo=UTC
    ))
    out = _call("garmin_daily", {})

    assert out["freshness"]["pipeline_enabled"] is False
    assert out["freshness"]["stale"] is True
    assert out["freshness"]["reasons"] == ["scheduled Garmin sync is disabled"]
