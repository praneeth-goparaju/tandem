from __future__ import annotations

import json
import logging
import os
import stat
from contextlib import contextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

from src.brain.atomic_io import (
    atomic_write_text,
    ensure_real_directory,
    read_regular_text,
)

log = logging.getLogger(__name__)

DAYS_BACK = 7


# The token store holds Garmin's "DI" identity trio (di_token, di_refresh_token,
# di_client_id). The access token lives ~19.5 hours and the refresh token ~30 days,
# and BOTH rotate on every refresh, so a refreshed pair must be written straight back
# to disk or the next run authenticates with a spent token.
#
# We refresh explicitly rather than relying on the client library: garth's automatic
# path (garth/http.py request(): `assert self.oauth1_token`) mints a new access token
# from an OAuth1 token, which this DI-format store does not contain. Under that path
# a token expiring between two daily runs would break the job silently. Refreshing
# here keeps the failure mode ours, and loud.
GARMIN_OAUTH_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
_REFRESH_MARGIN_SECONDS = 3600
_ACCESS_TTL_FALLBACK = 19 * 3600
_REFRESH_TTL_FALLBACK = 30 * 86400


def _token_path():
    from src.orchestrator import config

    store = config.GARMIN_TOKENSTORE
    try:
        info = store.lstat()
    except FileNotFoundError:
        return store
    if stat.S_ISLNK(info.st_mode):
        raise OSError(f"refusing symlink Garmin token store: {store}")
    return store / "garmin_tokens.json" if stat.S_ISDIR(info.st_mode) else store


@contextmanager
def _token_lock(path):
    import fcntl

    ensure_real_directory(path.parent)
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    locked = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"refusing unsafe Garmin token lock: {lock_path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError(f"Garmin token lock is not owned by this user: {lock_path}")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_token_store(path, data: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(data),
        mode=0o600,
        preserve_existing_mode=False,
    )


def refresh_tokens_if_needed(
    now: float | None = None, *, _already_locked: bool = False
) -> str:
    import time

    import httpx

    now = time.time() if now is None else now
    path = _token_path()
    if not _already_locked:
        with _token_lock(path):
            return refresh_tokens_if_needed(now, _already_locked=True)
    data = json.loads(read_regular_text(path))
    for key in ("di_token", "di_refresh_token", "di_client_id"):
        if not data.get(key):
            raise RuntimeError(
                f"garmin token store {path} is missing {key} — re-run the interactive login"
            )

    expires_at = float(data.get("expires_at") or 0)
    if expires_at and expires_at - now > _REFRESH_MARGIN_SECONDS:
        return "fresh"

    refresh_expires_at = float(data.get("refresh_expires_at") or 0)
    if refresh_expires_at and refresh_expires_at <= now:
        raise RuntimeError(
            f"garmin refresh token expired on "
            f"{datetime.fromtimestamp(refresh_expires_at):%Y-%m-%d} — re-run the interactive login"
        )

    resp = httpx.post(
        GARMIN_OAUTH_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": data["di_refresh_token"],
            "client_id": data["di_client_id"],
        },
        headers={"User-Agent": "GCM-iOS-5.7.2.1"},
        timeout=30,
        follow_redirects=False,
        trust_env=False,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"garmin token refresh failed with HTTP {resp.status_code} — "
            "the refresh token may be spent; re-run the interactive login"
        )
    body = resp.json()
    # Garmin has already invalidated the old refresh token by now, so the new pair
    # MUST reach disk. Absent/renamed lifetime fields fall back to conservative
    # defaults rather than 0: a 0 would stamp an expiry of "now", which makes the
    # next run either refresh needlessly or (for refresh_expires_at) refuse to run
    # with a bogus "token expired" error.
    data.update(
        {
            "di_token": body["access_token"],
            "di_refresh_token": body["refresh_token"],
            "expires_at": int(now) + int(body.get("expires_in") or _ACCESS_TTL_FALLBACK),
            "refresh_expires_at": int(now)
            + int(body.get("refresh_token_expires_in") or _REFRESH_TTL_FALLBACK),
        }
    )
    try:
        _write_token_store(path, data)
    except OSError as e:
        raise RuntimeError(
            f"garmin tokens were rotated at Garmin but could NOT be saved to {path} "
            f"({e}) — the store now holds a spent token; re-run the interactive login"
        ) from e
    log.info("garmin-sync: rotated tokens; access valid for %ss", body.get("expires_in"))
    return "refreshed"


def _restamp_expiry_metadata(*, _already_locked: bool = False) -> None:
    """Re-derive expires_at from the access token's own `exp` claim.

    The client library persists the token store itself on some paths, and its
    serializer keeps only the three di_* keys — so our expiry metadata is dropped.
    Without it every later run treats the token as expiring and burns a rotation.
    Decodes only the public JWT payload; no signature check, no token logged.
    """
    import base64
    import time

    path = _token_path()
    if not _already_locked:
        with _token_lock(path):
            return _restamp_expiry_metadata(_already_locked=True)
    try:
        data = json.loads(read_regular_text(path))
        if data.get("expires_at"):
            return
        payload = data["di_token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        data["expires_at"] = exp
        data.setdefault("refresh_expires_at", int(time.time()) + _REFRESH_TTL_FALLBACK)
        _write_token_store(path, data)
    except Exception as e:  # noqa: BLE001
        log.debug("garmin-sync: could not re-stamp token expiry: %s", e)


def _warn_if_relogin_due() -> None:
    import time

    try:
        data = json.loads(read_regular_text(_token_path()))
        left = (float(data.get("refresh_expires_at") or 0) - time.time()) / 86400
        if 0 < left <= 7:
            log.warning(
                "garmin-sync: the Garmin login expires in %d day(s) — re-run the "
                "interactive login (docs/INTEGRATIONS.md, Garmin > Setup)", int(left))
    except Exception:  # noqa: BLE001
        pass


def _default_garmin():
    from garminconnect import Garmin, GarminConnectAuthenticationError

    from src.orchestrator import config

    path = _token_path()
    with _token_lock(path):
        refresh_tokens_if_needed(_already_locked=True)
        client = Garmin()
        for session in (getattr(client, "cs", None), getattr(client, "_api_session", None)):
            if session is not None:
                session.trust_env = False
        try:
            client.login(str(config.GARMIN_TOKENSTORE))
        except GarminConnectAuthenticationError as e:
            # The library reports a dead token store as "Failed to retrieve social
            # profile", which reads like a transient Garmin hiccup. It is not: only a
            # human re-login fixes it, so say that.
            raise RuntimeError(
                "Garmin rejected the stored login — re-run the interactive login "
                f"(docs/INTEGRATIONS.md, Garmin > Setup). Original error: {e}"
            ) from e
        _restamp_expiry_metadata(_already_locked=True)
    _warn_if_relogin_due()
    return client


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _local_timestamp(gmt_iso: str | None, offset_minutes: int | None) -> str | None:
    """Garmin gives nap start/end as naive GMT plus a minute offset (the
    actual local UTC offset for that date, DST already applied) rather than
    a timezone name — add the offset directly instead of resolving a zone."""
    if not gmt_iso or offset_minutes is None:
        return None
    try:
        return (datetime.fromisoformat(gmt_iso) + timedelta(minutes=offset_minutes)).isoformat()
    except ValueError:
        return None


def _fetch_day(garmin, day: str) -> dict[str, Any] | None:
    failures = 0

    def fetch(label: str, fn, default):
        nonlocal failures
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.warning("garmin-sync: %s endpoint failed for %s: %s", label, day, exc)
            return default

    summary = fetch("summary", lambda: garmin.get_user_summary(day) or {}, None)
    sleep = fetch("sleep", lambda: garmin.get_sleep_data(day) or {}, None)
    hrv_data = fetch("hrv", lambda: garmin.get_hrv_data(day) or {}, None)
    activities = fetch(
        "activities", lambda: garmin.get_activities_by_date(day, day) or [], None
    )
    heart_rates = fetch("heart-rates", lambda: garmin.get_heart_rates(day) or {}, None)
    stress_detail = fetch("stress-detail", lambda: garmin.get_all_day_stress(day) or {}, None)
    body_battery = fetch("body-battery", lambda: garmin.get_body_battery(day, day) or [], None)
    body_battery_events = fetch(
        "body-battery-events", lambda: garmin.get_body_battery_events(day) or [], None
    )
    respiration = fetch("respiration", lambda: garmin.get_respiration_data(day) or {}, None)
    spo2 = fetch("spo2", lambda: garmin.get_spo2_data(day) or {}, None)
    intensity = fetch(
        "intensity-minutes", lambda: garmin.get_intensity_minutes_data(day) or {}, None
    )
    training_readiness = fetch(
        "training-readiness", lambda: garmin.get_training_readiness(day) or [], None
    )
    training_status = fetch(
        "training-status", lambda: garmin.get_training_status(day) or {}, None
    )
    endurance_score = fetch(
        "endurance-score", lambda: garmin.get_endurance_score(day, day) or {}, None
    )
    fitness_age = fetch("fitness-age", lambda: garmin.get_fitnessage_data(day) or {}, None)
    floors = fetch("floors", lambda: garmin.get_floors(day) or {}, None)
    hydration = fetch("hydration", lambda: garmin.get_hydration_data(day) or {}, None)
    body_composition = fetch(
        "body-composition", lambda: garmin.get_body_composition(day, day) or {}, None
    )
    endpoint_count = 18
    if failures == endpoint_count:
        raise RuntimeError("all Garmin endpoints failed")
    summary = summary or {}
    sleep_dto = _dig(sleep or {}, "dailySleepDTO") or {}
    hrv = _dig(hrv_data or {}, "hrvSummary") or {}
    hr_values = (heart_rates or {}).get("heartRateValues") or []
    measured_hr_values = [
        sample for sample in hr_values
        if isinstance(sample, (list, tuple)) and len(sample) > 1
        and sample[1] is not None
    ]
    training = _dig(training_status or {}, "mostRecentTrainingStatus", "latestTrainingStatusData") or {}
    training_device: dict[str, Any] = (
        next(iter(training.values()), {}) if isinstance(training, dict) else {}
    )
    acute = training_device.get("acuteTrainingLoadDTO") or {}
    vo2 = _dig(training_status or {}, "mostRecentVO2Max", "generic") or {}
    body_avg = (body_composition or {}).get("totalAverage") or {}

    naps = sleep_dto.get("dailyNapDTOS") or []
    nap_start_local = nap_end_local = nap_feedback = None
    if naps:
        first, last = naps[0], naps[-1]
        nap_start_local = _local_timestamp(
            first.get("napStartTimestampGMT"), first.get("napStartTimeOffset")
        )
        nap_end_local = _local_timestamp(
            last.get("napEndTimestampGMT"), last.get("napEndTimeOffset")
        )
        feedbacks = [n["napFeedback"] for n in naps if n.get("napFeedback")]
        nap_feedback = "; ".join(dict.fromkeys(feedbacks)) or None

    row = {
        "date": day,
        "steps": summary.get("totalSteps"),
        "resting_hr": summary.get("restingHeartRate"),
        "sleep_seconds": sleep_dto.get("sleepTimeSeconds"),
        "sleep_score": _dig(sleep_dto, "sleepScores", "overall", "value"),
        "deep_sleep_seconds": sleep_dto.get("deepSleepSeconds"),
        "light_sleep_seconds": sleep_dto.get("lightSleepSeconds"),
        "rem_sleep_seconds": sleep_dto.get("remSleepSeconds"),
        "awake_sleep_seconds": sleep_dto.get("awakeSleepSeconds"),
        "nap_seconds": sleep_dto.get("napTimeSeconds"),
        "nap_start_local": nap_start_local,
        "nap_end_local": nap_end_local,
        "nap_feedback": nap_feedback,
        "hrv_last_night_avg": hrv.get("lastNightAvg"),
        "hrv_status": hrv.get("status"),
        "stress_avg": summary.get("averageStressLevel"),
        "body_battery_charged": summary.get("bodyBatteryChargedValue"),
        "body_battery_drained": summary.get("bodyBatteryDrainedValue"),
        "active_kilocalories": summary.get("activeKilocalories"),
        "total_kilocalories": summary.get("totalKilocalories"),
        "heart_rate_min": (heart_rates or {}).get("minHeartRate"),
        "heart_rate_max": (heart_rates or {}).get("maxHeartRate"),
        "heart_rate_samples": len(measured_hr_values) if measured_hr_values else None,
        "respiration_lowest": (respiration or {}).get("lowestRespirationValue"),
        "respiration_highest": (respiration or {}).get("highestRespirationValue"),
        "respiration_awake_avg": (respiration or {}).get("avgWakingRespirationValue"),
        "respiration_sleep_avg": (respiration or {}).get("avgSleepRespirationValue"),
        "spo2_avg": (spo2 or {}).get("averageSpO2"),
        "spo2_lowest": (spo2 or {}).get("lowestSpO2"),
        "spo2_sleep_avg": (spo2 or {}).get("avgSleepSpO2"),
        "intensity_moderate_minutes": (intensity or {}).get("moderateMinutes"),
        "intensity_vigorous_minutes": (intensity or {}).get("vigorousMinutes"),
        "intensity_weekly_total": (intensity or {}).get("weeklyTotal"),
        "intensity_weekly_goal": (intensity or {}).get("weekGoal"),
        "vo2_max": vo2.get("vo2MaxPreciseValue"),
        "fitness_age": (fitness_age or {}).get("fitnessAge"),
        "achievable_fitness_age": (fitness_age or {}).get("achievableFitnessAge"),
        "training_status": training_device.get("trainingStatusFeedbackPhrase"),
        "acute_training_load": acute.get("dailyTrainingLoadAcute"),
        "chronic_training_load": acute.get("dailyTrainingLoadChronic"),
        "hydration_ml": (hydration or {}).get("valueInML"),
        "hydration_goal_ml": (hydration or {}).get("goalInML"),
        "weight_kg": body_avg.get("weight"),
        "bmi": body_avg.get("bmi"),
        "body_fat_pct": body_avg.get("bodyFat"),
        "body_water_pct": body_avg.get("bodyWater"),
        "muscle_mass_kg": body_avg.get("muscleMass"),
        "health_details": json.dumps({
            "heart_rates": heart_rates,
            "stress": stress_detail,
            "body_battery": body_battery,
            "body_battery_events": body_battery_events,
            "respiration": respiration,
            "spo2": spo2,
            "intensity_minutes": intensity,
            "training_readiness": training_readiness,
            "training_status": training_status,
            "endurance_score": endurance_score,
            "fitness_age": fitness_age,
            "floors": floors,
            "hydration": hydration,
            "body_composition": body_composition,
        }),
        "activities": json.dumps([
            {
                "id": a.get("activityId"),
                "name": a.get("activityName"),
                "type": _dig(a, "activityType", "typeKey"),
                "start_local": a.get("startTimeLocal"),
                "duration_s": a.get("duration"),
                "distance_m": a.get("distance"),
                "calories": a.get("calories"),
                "average_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "average_speed_mps": a.get("averageSpeed"),
                "max_speed_mps": a.get("maxSpeed"),
                "elevation_gain_m": a.get("elevationGain"),
                "average_cadence": a.get("averageRunningCadenceInStepsPerMinute"),
                "aerobic_training_effect": a.get("aerobicTrainingEffect"),
                "anaerobic_training_effect": a.get("anaerobicTrainingEffect"),
            }
            for a in (activities or [])
        ]) if activities is not None else None,
    }

    # Garmin reports averageStressLevel = -1 for "not measured".
    if isinstance(row["stress_avg"], int) and row["stress_avg"] < 0:
        row["stress_avg"] = None

    has_data = any(
        row[k] is not None
        for k in ("steps", "resting_hr", "sleep_seconds", "hrv_last_night_avg",
                   "nap_seconds")
    ) or bool(activities) or any(
        value is not None
        for value in (
            row["heart_rate_samples"], row["respiration_awake_avg"],
            row["hydration_ml"], row["weight_kg"],
        )
    )
    return row if has_data else None


def _upsert(row: dict[str, Any]) -> None:
    from src.db import get_db, utcnow

    conn = get_db()
    conn.execute(
        """INSERT INTO garmin_days (date, steps, resting_hr, sleep_seconds,
             sleep_score, deep_sleep_seconds, light_sleep_seconds,
             rem_sleep_seconds, awake_sleep_seconds, nap_seconds,
             nap_start_local, nap_end_local, nap_feedback,
             hrv_last_night_avg, hrv_status, stress_avg,
             body_battery_charged, body_battery_drained, active_kilocalories,
             total_kilocalories, heart_rate_min, heart_rate_max,
             heart_rate_samples, respiration_lowest, respiration_highest,
             respiration_awake_avg, respiration_sleep_avg, spo2_avg,
             spo2_lowest, spo2_sleep_avg, intensity_moderate_minutes,
             intensity_vigorous_minutes, intensity_weekly_total,
             intensity_weekly_goal, vo2_max, fitness_age,
             achievable_fitness_age, training_status, acute_training_load,
             chronic_training_load, hydration_ml, hydration_goal_ml, weight_kg,
             bmi, body_fat_pct, body_water_pct, muscle_mass_kg, health_details,
             activities, synced_at)
           VALUES (:date, :steps, :resting_hr, :sleep_seconds, :sleep_score,
             :deep_sleep_seconds, :light_sleep_seconds, :rem_sleep_seconds,
             :awake_sleep_seconds, :nap_seconds, :nap_start_local,
             :nap_end_local, :nap_feedback,
             :hrv_last_night_avg, :hrv_status, :stress_avg,
             :body_battery_charged, :body_battery_drained,
             :active_kilocalories, :total_kilocalories, :heart_rate_min,
             :heart_rate_max, :heart_rate_samples, :respiration_lowest,
             :respiration_highest, :respiration_awake_avg,
             :respiration_sleep_avg, :spo2_avg, :spo2_lowest, :spo2_sleep_avg,
             :intensity_moderate_minutes, :intensity_vigorous_minutes,
             :intensity_weekly_total, :intensity_weekly_goal, :vo2_max,
             :fitness_age, :achievable_fitness_age, :training_status,
             :acute_training_load, :chronic_training_load, :hydration_ml,
             :hydration_goal_ml, :weight_kg, :bmi, :body_fat_pct,
             :body_water_pct, :muscle_mass_kg, :health_details, :activities,
             :synced_at)
           -- COALESCE, not a bare overwrite: _fetch_day calls four independent
           -- endpoints, so a run where the sleep call fails would otherwise NULL
           -- out sleep data an earlier run had already captured. New readings win;
           -- a missing reading leaves the stored one alone.
           ON CONFLICT(date) DO UPDATE SET
             steps=COALESCE(excluded.steps, garmin_days.steps),
             resting_hr=COALESCE(excluded.resting_hr, garmin_days.resting_hr),
             sleep_seconds=COALESCE(excluded.sleep_seconds, garmin_days.sleep_seconds),
             sleep_score=COALESCE(excluded.sleep_score, garmin_days.sleep_score),
             deep_sleep_seconds=COALESCE(excluded.deep_sleep_seconds,
               garmin_days.deep_sleep_seconds),
             light_sleep_seconds=COALESCE(excluded.light_sleep_seconds,
               garmin_days.light_sleep_seconds),
             rem_sleep_seconds=COALESCE(excluded.rem_sleep_seconds,
               garmin_days.rem_sleep_seconds),
             awake_sleep_seconds=COALESCE(excluded.awake_sleep_seconds,
               garmin_days.awake_sleep_seconds),
             nap_seconds=COALESCE(excluded.nap_seconds, garmin_days.nap_seconds),
             nap_start_local=COALESCE(excluded.nap_start_local,
               garmin_days.nap_start_local),
             nap_end_local=COALESCE(excluded.nap_end_local,
               garmin_days.nap_end_local),
             nap_feedback=COALESCE(excluded.nap_feedback, garmin_days.nap_feedback),
             hrv_last_night_avg=COALESCE(excluded.hrv_last_night_avg,
               garmin_days.hrv_last_night_avg),
             hrv_status=COALESCE(excluded.hrv_status, garmin_days.hrv_status),
             stress_avg=COALESCE(excluded.stress_avg, garmin_days.stress_avg),
             body_battery_charged=COALESCE(excluded.body_battery_charged,
               garmin_days.body_battery_charged),
             body_battery_drained=COALESCE(excluded.body_battery_drained,
               garmin_days.body_battery_drained),
             active_kilocalories=COALESCE(excluded.active_kilocalories,
               garmin_days.active_kilocalories),
             total_kilocalories=COALESCE(excluded.total_kilocalories,
               garmin_days.total_kilocalories),
             heart_rate_min=COALESCE(excluded.heart_rate_min, garmin_days.heart_rate_min),
             heart_rate_max=COALESCE(excluded.heart_rate_max, garmin_days.heart_rate_max),
             heart_rate_samples=COALESCE(excluded.heart_rate_samples, garmin_days.heart_rate_samples),
             respiration_lowest=COALESCE(excluded.respiration_lowest, garmin_days.respiration_lowest),
             respiration_highest=COALESCE(excluded.respiration_highest, garmin_days.respiration_highest),
             respiration_awake_avg=COALESCE(excluded.respiration_awake_avg, garmin_days.respiration_awake_avg),
             respiration_sleep_avg=COALESCE(excluded.respiration_sleep_avg, garmin_days.respiration_sleep_avg),
             spo2_avg=COALESCE(excluded.spo2_avg, garmin_days.spo2_avg),
             spo2_lowest=COALESCE(excluded.spo2_lowest, garmin_days.spo2_lowest),
             spo2_sleep_avg=COALESCE(excluded.spo2_sleep_avg, garmin_days.spo2_sleep_avg),
             intensity_moderate_minutes=COALESCE(excluded.intensity_moderate_minutes, garmin_days.intensity_moderate_minutes),
             intensity_vigorous_minutes=COALESCE(excluded.intensity_vigorous_minutes, garmin_days.intensity_vigorous_minutes),
             intensity_weekly_total=COALESCE(excluded.intensity_weekly_total, garmin_days.intensity_weekly_total),
             intensity_weekly_goal=COALESCE(excluded.intensity_weekly_goal, garmin_days.intensity_weekly_goal),
             vo2_max=COALESCE(excluded.vo2_max, garmin_days.vo2_max),
             fitness_age=COALESCE(excluded.fitness_age, garmin_days.fitness_age),
             achievable_fitness_age=COALESCE(excluded.achievable_fitness_age, garmin_days.achievable_fitness_age),
             training_status=COALESCE(excluded.training_status, garmin_days.training_status),
             acute_training_load=COALESCE(excluded.acute_training_load, garmin_days.acute_training_load),
             chronic_training_load=COALESCE(excluded.chronic_training_load, garmin_days.chronic_training_load),
             hydration_ml=COALESCE(excluded.hydration_ml, garmin_days.hydration_ml),
             hydration_goal_ml=COALESCE(excluded.hydration_goal_ml, garmin_days.hydration_goal_ml),
             weight_kg=COALESCE(excluded.weight_kg, garmin_days.weight_kg),
             bmi=COALESCE(excluded.bmi, garmin_days.bmi),
             body_fat_pct=COALESCE(excluded.body_fat_pct, garmin_days.body_fat_pct),
             body_water_pct=COALESCE(excluded.body_water_pct, garmin_days.body_water_pct),
             muscle_mass_kg=COALESCE(excluded.muscle_mass_kg, garmin_days.muscle_mass_kg),
             health_details=COALESCE(excluded.health_details, garmin_days.health_details),
             activities=COALESCE(excluded.activities, garmin_days.activities),
             synced_at=excluded.synced_at""",
        {**row, "synced_at": utcnow()},
    )
    conn.commit()


def run_once(
    garmin=None,
    days_back: int = DAYS_BACK,
    today: str | None = None,
    include_today: bool = False,
) -> dict[str, int]:
    """One sync pass. Returns {"days_synced": n, "fetch_errors": m}.

    Raises on login/client construction failure (the scheduler wrapper logs
    and skips the success stamp); per-day failures only log.
    """
    if garmin is None:
        garmin = _default_garmin()

    anchor = (
        datetime.strptime(today, "%Y-%m-%d").date() if today else date_cls.today()
    )

    days_synced = 0
    fetch_errors = 0
    # The scheduled 07:00 sync passes include_today, because last night's sleep
    # is filed under today's date; without it the window starts at yesterday.
    offsets = range(0, days_back) if include_today else range(1, days_back + 1)
    for offset in offsets:
        day = (anchor - timedelta(days=offset)).isoformat()
        try:
            row = _fetch_day(garmin, day)
        except Exception as e:  # noqa: BLE001
            log.warning("garmin-sync: fetch failed for %s: %s", day, e)
            fetch_errors += 1
            continue
        if row is None:
            continue
        _upsert(row)
        days_synced += 1

    if fetch_errors and days_synced == 0:
        raise RuntimeError(
            f"garmin-sync: all {fetch_errors} day(s) failed to fetch — "
            "treating as an outage, not an empty week"
        )
    log.info("garmin-sync: synced %d day(s)", days_synced)
    return {"days_synced": days_synced, "fetch_errors": fetch_errors}


def main() -> None:
    from src.db import get_db, utcnow
    from src.orchestrator.config import GARMIN_ENABLED

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not GARMIN_ENABLED:
        print("garmin sync is off (set GARMIN_ENABLED=true to turn it on)")
        return
    counts = run_once(days_back=7, include_today=True)
    conn = get_db()
    conn.execute(
        "INSERT INTO daemon_state(key, value) VALUES('last_garmin_sync', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (utcnow(),),
    )
    conn.commit()
    print(counts)


if __name__ == "__main__":
    main()
