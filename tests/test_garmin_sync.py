from __future__ import annotations

import json


def _summary(steps=None, rhr=None, **extra):
    base = {
        "totalSteps": steps,
        "restingHeartRate": rhr,
        "activeKilocalories": None,
        "totalKilocalories": None,
        "averageStressLevel": None,
        "bodyBatteryChargedValue": None,
        "bodyBatteryDrainedValue": None,
    }
    base.update(extra)
    return base


def _sleep(seconds=None, score=None, deep=None, light=None, rem=None,
           awake=None, nap_seconds=None, naps=None):
    dto = {}
    if seconds is not None:
        dto["sleepTimeSeconds"] = seconds
    if score is not None:
        dto["sleepScores"] = {"overall": {"value": score}}
    if deep is not None:
        dto["deepSleepSeconds"] = deep
    if light is not None:
        dto["lightSleepSeconds"] = light
    if rem is not None:
        dto["remSleepSeconds"] = rem
    if awake is not None:
        dto["awakeSleepSeconds"] = awake
    if nap_seconds is not None:
        dto["napTimeSeconds"] = nap_seconds
    if naps is not None:
        dto["dailyNapDTOS"] = naps
    return {"dailySleepDTO": dto}


def _nap(seconds, start_gmt, end_gmt, offset=120, feedback=None):
    """One dailyNapDTOS entry — Garmin gives naive GMT plus a minute offset
    (the actual local UTC offset for that date), not a timezone name."""
    return {
        "napTimeSec": seconds,
        "napStartTimestampGMT": start_gmt,
        "napEndTimestampGMT": end_gmt,
        "napStartTimeOffset": offset,
        "napEndTimeOffset": offset,
        "napFeedback": feedback,
    }


def _hrv(avg=None, status=None):
    if avg is None and status is None:
        return None
    return {"hrvSummary": {"lastNightAvg": avg, "status": status}}


class FakeGarmin:
    def __init__(self, days: dict[str, dict] | None = None,
                 fail_dates: set[str] | None = None) -> None:
        self.days = days or {}
        self.fail_dates = fail_dates or set()

    def _day(self, date: str) -> dict:
        if date in self.fail_dates:
            raise RuntimeError(f"garmin API error for {date}")
        return self.days.get(date, {})

    def get_user_summary(self, date: str) -> dict:
        return self._day(date).get("summary", _summary())

    def get_sleep_data(self, date: str) -> dict:
        return self._day(date).get("sleep", _sleep())

    def get_hrv_data(self, date: str):
        return self._day(date).get("hrv", _hrv())

    def get_activities_by_date(self, start: str, end: str) -> list:
        return self._day(start).get("activities", [])

    def get_heart_rates(self, date: str) -> dict:
        return self._day(date).get("heart_rates", {})

    def get_all_day_stress(self, date: str) -> dict:
        return self._day(date).get("stress_detail", {})

    def get_body_battery(self, start: str, end: str) -> list:
        return self._day(start).get("body_battery", [])

    def get_body_battery_events(self, date: str) -> list:
        return self._day(date).get("body_battery_events", [])

    def get_respiration_data(self, date: str) -> dict:
        return self._day(date).get("respiration", {})

    def get_spo2_data(self, date: str) -> dict:
        return self._day(date).get("spo2", {})

    def get_intensity_minutes_data(self, date: str) -> dict:
        return self._day(date).get("intensity", {})

    def get_training_readiness(self, date: str) -> list:
        return self._day(date).get("training_readiness", [])

    def get_training_status(self, date: str) -> dict:
        return self._day(date).get("training_status", {})

    def get_endurance_score(self, start: str, end: str) -> dict:
        return self._day(start).get("endurance_score", {})

    def get_fitnessage_data(self, date: str) -> dict:
        return self._day(date).get("fitness_age", {})

    def get_floors(self, date: str) -> dict:
        return self._day(date).get("floors", {})

    def get_hydration_data(self, date: str) -> dict:
        return self._day(date).get("hydration", {})

    def get_body_composition(self, start: str, end: str) -> dict:
        return self._day(start).get("body_composition", {})


def _run(isolated_db, garmin, **kw):
    from src.orchestrator import garmin_sync

    return garmin_sync.run_once(garmin=garmin, today="2026-08-20", **kw)


def _rows(isolated_db):
    conn = isolated_db.get_db()
    cur = conn.execute("SELECT * FROM garmin_days ORDER BY date")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_syncs_day_into_garmin_days(isolated_db):
    garmin = FakeGarmin({
        "2026-08-19": {
            "summary": _summary(steps=8949, rhr=84, activeKilocalories=412,
                                averageStressLevel=31,
                                bodyBatteryChargedValue=55,
                                bodyBatteryDrainedValue=60),
            "sleep": _sleep(seconds=6 * 3600, score=72),
            "hrv": _hrv(avg=38, status="BALANCED"),
            "activities": [{"activityType": {"typeKey": "walking"},
                            "duration": 3600.0, "distance": 5200.0}],
        },
    })
    counts = _run(isolated_db, garmin, days_back=2)

    assert counts["days_synced"] == 1
    (row,) = _rows(isolated_db)
    assert row["date"] == "2026-08-19"
    assert row["steps"] == 8949
    assert row["resting_hr"] == 84
    assert row["sleep_seconds"] == 6 * 3600
    assert row["sleep_score"] == 72
    assert row["hrv_last_night_avg"] == 38
    assert row["hrv_status"] == "BALANCED"
    assert row["stress_avg"] == 31
    assert row["body_battery_charged"] == 55
    assert row["body_battery_drained"] == 60
    assert row["active_kilocalories"] == 412
    acts = json.loads(row["activities"])
    assert acts[0]["type"] == "walking"


def test_skips_days_with_no_data(isolated_db):
    counts = _run(isolated_db, FakeGarmin(), days_back=3)
    assert counts["days_synced"] == 0
    assert _rows(isolated_db) == []


def test_upsert_refreshes_existing_row(isolated_db):
    garmin = FakeGarmin({"2026-08-19": {"summary": _summary(steps=100)}})
    _run(isolated_db, garmin, days_back=2)
    garmin.days["2026-08-19"]["summary"] = _summary(steps=4500, rhr=80)
    _run(isolated_db, garmin, days_back=2)

    (row,) = _rows(isolated_db)
    assert row["steps"] == 4500
    assert row["resting_hr"] == 80


def test_zero_step_days_are_still_stored(isolated_db):
    garmin = FakeGarmin({"2026-08-19": {"summary": _summary(steps=0, rhr=80)}})
    counts = _run(isolated_db, garmin, days_back=2)

    assert counts["days_synced"] == 1
    assert _rows(isolated_db)[0]["resting_hr"] == 80


def test_per_day_failure_is_skipped_not_fatal(isolated_db):
    garmin = FakeGarmin(
        {"2026-08-18": {"summary": _summary(steps=1459, rhr=78)}},
        fail_dates={"2026-08-19"},
    )
    counts = _run(isolated_db, garmin, days_back=3)

    assert counts["days_synced"] == 1
    (row,) = _rows(isolated_db)
    assert row["date"] == "2026-08-18"


def test_one_endpoint_failure_preserves_other_metrics(isolated_db):
    class PartialGarmin(FakeGarmin):
        def get_sleep_data(self, date: str) -> dict:
            raise RuntimeError("sleep endpoint unavailable")

    garmin = PartialGarmin({
        "2026-08-19": {
            "summary": _summary(steps=3210, rhr=70),
            "hrv": _hrv(avg=42, status="BALANCED"),
        },
    })
    counts = _run(isolated_db, garmin, days_back=1)

    assert counts["days_synced"] == 1
    row = _rows(isolated_db)[0]
    assert row["steps"] == 3210
    assert row["sleep_seconds"] is None
    assert row["hrv_last_night_avg"] == 42


def test_syncs_sleep_stage_and_nap_detail(isolated_db):
    garmin = FakeGarmin({
        "2026-08-19": {
            "summary": _summary(steps=1713, rhr=73),
            "sleep": _sleep(
                deep=5400, light=10800, rem=3600, awake=600,
                nap_seconds=4860,
                naps=[_nap(
                    4860, "2026-08-19T13:11:56", "2026-08-19T14:32:56",
                    offset=120, feedback="LONG_DURATION_LOW_NEED",
                )],
            ),
        },
    })
    _run(isolated_db, garmin, days_back=1)

    (row,) = _rows(isolated_db)
    assert row["deep_sleep_seconds"] == 5400
    assert row["light_sleep_seconds"] == 10800
    assert row["rem_sleep_seconds"] == 3600
    assert row["awake_sleep_seconds"] == 600
    assert row["nap_seconds"] == 4860
    assert row["nap_start_local"] == "2026-08-19T15:11:56"
    assert row["nap_end_local"] == "2026-08-19T16:32:56"
    assert row["nap_feedback"] == "LONG_DURATION_LOW_NEED"


def test_syncs_extended_health_and_lossless_details(isolated_db):
    garmin = FakeGarmin({
        "2026-08-19": {
            "summary": _summary(steps=100),
            "heart_rates": {
                "minHeartRate": 58, "maxHeartRate": 142,
                "heartRateValues": [[0, None], [1, 58], [2, 142]],
            },
            "respiration": {
                "lowestRespirationValue": 10.0,
                "highestRespirationValue": 23.0,
                "avgWakingRespirationValue": 17.0,
                "avgSleepRespirationValue": 15.0,
            },
            "spo2": {"averageSpO2": 97.0, "lowestSpO2": 92.0,
                     "avgSleepSpO2": 96.0},
            "intensity": {"moderateMinutes": 12, "vigorousMinutes": 4,
                          "weeklyTotal": 20, "weekGoal": 150},
            "training_status": {
                "mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 31.2}},
                "mostRecentTrainingStatus": {"latestTrainingStatusData": {
                    "device": {"trainingStatusFeedbackPhrase": "PRODUCTIVE",
                               "acuteTrainingLoadDTO": {
                                   "dailyTrainingLoadAcute": 70,
                                   "dailyTrainingLoadChronic": 100,
                               }}
                }},
            },
            "fitness_age": {"fitnessAge": 39.5, "achievableFitnessAge": 34.0},
            "hydration": {"valueInML": 1200, "goalInML": 2800},
        },
    })
    _run(isolated_db, garmin, days_back=1)

    (row,) = _rows(isolated_db)
    assert row["heart_rate_min"] == 58
    assert row["heart_rate_samples"] == 2
    assert row["respiration_sleep_avg"] == 15.0
    assert row["spo2_avg"] == 97.0
    assert row["intensity_weekly_goal"] == 150
    assert row["vo2_max"] == 31.2
    assert row["training_status"] == "PRODUCTIVE"
    assert row["fitness_age"] == 39.5
    assert row["hydration_ml"] == 1200
    details = json.loads(row["health_details"])
    assert details["heart_rates"]["heartRateValues"] == [
        [0, None], [1, 58], [2, 142]
    ]
    assert "training_readiness" in details


def test_profile_only_metrics_do_not_create_a_false_daily_record(isolated_db):
    """Garmin repeats VO2 max, fitness age and training status on dates where
    the watch recorded nothing. Those profile snapshots must not erase the
    distinction between a recorded day and a missing day."""
    garmin = FakeGarmin({
        "2026-08-19": {
            "training_status": {
                "mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 30.0}},
            },
            "fitness_age": {"fitnessAge": 40.0},
        },
    })
    counts = _run(isolated_db, garmin, days_back=1)

    assert counts["days_synced"] == 0
    assert _rows(isolated_db) == []


def test_nap_only_day_is_not_treated_as_empty(isolated_db):
    garmin = FakeGarmin({
        "2026-08-19": {
            "sleep": _sleep(nap_seconds=1800, naps=[_nap(
                1800, "2026-08-19T13:00:00", "2026-08-19T13:30:00",
            )]),
        },
    })
    counts = _run(isolated_db, garmin, days_back=1)

    assert counts["days_synced"] == 1
    (row,) = _rows(isolated_db)
    assert row["nap_seconds"] == 1800
    assert row["steps"] is None


def test_multiple_naps_span_first_start_to_last_end(isolated_db):
    garmin = FakeGarmin({
        "2026-08-19": {
            "summary": _summary(steps=100),
            "sleep": _sleep(nap_seconds=2700, naps=[
                _nap(900, "2026-08-19T10:00:00", "2026-08-19T10:15:00",
                     feedback="SHORT_NAP"),
                _nap(1800, "2026-08-19T13:00:00", "2026-08-19T13:30:00",
                     feedback="GOOD_NAP"),
            ]),
        },
    })
    _run(isolated_db, garmin, days_back=1)

    (row,) = _rows(isolated_db)
    assert row["nap_start_local"] == "2026-08-19T12:00:00"
    assert row["nap_end_local"] == "2026-08-19T15:30:00"
    assert row["nap_feedback"] == "SHORT_NAP; GOOD_NAP"


# ---------------------------------------------------------------- token rotation ----
# Garmin's DI access token lives ~19.5h and the refresh token ~30 days, and BOTH rotate
# on use, so a refreshed pair must land on disk or the next daily run authenticates with
# a spent token. These tests pin that contract; no network, no real token store.


def _token_store(tmp_path, **over):
    import json as _json
    import time as _time

    data = {
        "di_token": "access-old",
        "di_refresh_token": "refresh-old",
        "di_client_id": "client-1",
        "expires_at": int(_time.time()) + 40000,
        "refresh_expires_at": int(_time.time()) + 2000000,
    }
    data.update(over)
    p = tmp_path / "garmin_tokens.json"
    p.write_text(_json.dumps(data))
    return p


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_refresh_skipped_while_the_access_token_is_still_good(tmp_path, monkeypatch):
    from src.orchestrator import garmin_sync

    p = _token_store(tmp_path)
    monkeypatch.setattr(garmin_sync, "_token_path", lambda: p)

    def _boom(*a, **k):
        raise AssertionError("refresh attempted while the token was still valid")

    monkeypatch.setattr("httpx.post", _boom)
    assert garmin_sync.refresh_tokens_if_needed() == "fresh"


def test_refresh_rotates_both_tokens_and_persists_them(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    from src.orchestrator import garmin_sync

    p = _token_store(tmp_path, expires_at=int(_time.time()) + 60)
    monkeypatch.setattr(garmin_sync, "_token_path", lambda: p)
    sent = {}

    def _post(
        url, data=None, headers=None, timeout=None, follow_redirects=None, trust_env=None
    ):
        sent["url"] = url
        sent["data"] = data
        sent["trust_env"] = trust_env
        return _Resp(200, {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_in": 70000,
            "refresh_token_expires_in": 2592000,
        })

    monkeypatch.setattr("httpx.post", _post)
    assert garmin_sync.refresh_tokens_if_needed() == "refreshed"

    assert sent["url"] == garmin_sync.GARMIN_OAUTH_URL
    assert sent["data"]["refresh_token"] == "refresh-old"
    assert sent["data"]["grant_type"] == "refresh_token"
    assert sent["trust_env"] is False

    saved = _json.loads(p.read_text())
    assert saved["di_token"] == "access-new"
    assert saved["di_refresh_token"] == "refresh-new"
    assert saved["di_client_id"] == "client-1"
    assert saved["expires_at"] > _time.time()
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_refresh_failure_raises_and_leaves_the_store_untouched(tmp_path, monkeypatch):
    import json as _json
    import time as _time

    from src.orchestrator import garmin_sync

    p = _token_store(tmp_path, expires_at=int(_time.time()) + 60)
    monkeypatch.setattr(garmin_sync, "_token_path", lambda: p)
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(400, {}))

    try:
        garmin_sync.refresh_tokens_if_needed()
        raise AssertionError("expected a loud failure")
    except RuntimeError as e:
        assert "re-run the interactive login" in str(e)
    assert _json.loads(p.read_text())["di_refresh_token"] == "refresh-old"


def test_expired_refresh_token_fails_loudly_without_calling_garmin(tmp_path, monkeypatch):
    import time as _time

    from src.orchestrator import garmin_sync

    p = _token_store(
        tmp_path,
        expires_at=int(_time.time()) - 10,
        refresh_expires_at=int(_time.time()) - 10,
    )
    monkeypatch.setattr(garmin_sync, "_token_path", lambda: p)
    monkeypatch.setattr("httpx.post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call Garmin with a dead refresh token")))

    try:
        garmin_sync.refresh_tokens_if_needed()
        raise AssertionError("expected a loud failure")
    except RuntimeError as e:
        assert "re-run the interactive login" in str(e)


def test_missing_token_field_names_the_fix(tmp_path, monkeypatch):
    import json as _json

    from src.orchestrator import garmin_sync

    p = tmp_path / "garmin_tokens.json"
    p.write_text(_json.dumps({"di_token": "x", "di_client_id": "y"}))
    monkeypatch.setattr(garmin_sync, "_token_path", lambda: p)
    try:
        garmin_sync.refresh_tokens_if_needed()
        raise AssertionError("expected a loud failure")
    except RuntimeError as e:
        assert "di_refresh_token" in str(e)


def test_default_garmin_disables_proxy_environment_on_library_sessions(
    monkeypatch, tmp_path
) -> None:
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace

    from src.orchestrator import garmin_sync

    client = SimpleNamespace(
        cs=SimpleNamespace(trust_env=True),
        _api_session=SimpleNamespace(trust_env=True),
        login=lambda _path: None,
    )
    monkeypatch.setitem(sys.modules, "garminconnect", SimpleNamespace(
        Garmin=lambda: client,
        GarminConnectAuthenticationError=RuntimeError,
    ))
    monkeypatch.setattr(garmin_sync, "_token_path", lambda: tmp_path / "tokens.json")
    monkeypatch.setattr(garmin_sync, "_token_lock", lambda _path: nullcontext())
    monkeypatch.setattr(garmin_sync, "refresh_tokens_if_needed", lambda **_kwargs: None)
    monkeypatch.setattr(garmin_sync, "_restamp_expiry_metadata", lambda **_kwargs: None)
    monkeypatch.setattr(garmin_sync, "_warn_if_relogin_due", lambda: None)

    assert garmin_sync._default_garmin() is client
    assert client.cs.trust_env is False
    assert client._api_session.trust_env is False


def test_main_syncs_a_week_with_today_and_stamps_the_sync(isolated_db, monkeypatch):
    from src.orchestrator import config, garmin_sync

    calls = []
    monkeypatch.setattr(config, "GARMIN_ENABLED", True)
    monkeypatch.setattr(
        garmin_sync, "run_once", lambda **kw: calls.append(kw) or {"days_synced": 7, "fetch_errors": 0}
    )
    garmin_sync.main()
    assert calls == [{"days_back": 7, "include_today": True}]
    stamp = isolated_db.get_db().execute(
        "SELECT value FROM daemon_state WHERE key = 'last_garmin_sync'"
    ).fetchone()
    assert stamp is not None


def test_main_does_nothing_when_garmin_is_off(isolated_db, monkeypatch, capsys):
    from src.orchestrator import config, garmin_sync

    monkeypatch.setattr(config, "GARMIN_ENABLED", False)
    monkeypatch.setattr(garmin_sync, "run_once", lambda **kw: (_ for _ in ()).throw(AssertionError))
    garmin_sync.main()
    assert "off" in capsys.readouterr().out
