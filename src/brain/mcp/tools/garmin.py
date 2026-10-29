from __future__ import annotations

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from src.brain.mcp.tool_registry import brain_tool
from src.db import get_db

_COLUMNS = (
    "date", "steps", "resting_hr", "sleep_seconds", "sleep_score",
    "deep_sleep_seconds", "light_sleep_seconds", "rem_sleep_seconds",
    "awake_sleep_seconds", "nap_seconds", "nap_start_local", "nap_end_local",
    "nap_feedback", "hrv_last_night_avg", "hrv_status", "stress_avg",
    "body_battery_charged", "body_battery_drained", "active_kilocalories",
    "total_kilocalories", "activities", "synced_at",
    "heart_rate_min", "heart_rate_max", "heart_rate_samples",
    "respiration_lowest", "respiration_highest", "respiration_awake_avg",
    "respiration_sleep_avg", "spo2_avg", "spo2_lowest", "spo2_sleep_avg",
    "intensity_moderate_minutes", "intensity_vigorous_minutes",
    "intensity_weekly_total", "intensity_weekly_goal", "vo2_max",
    "fitness_age", "achievable_fitness_age", "training_status",
    "acute_training_load", "chronic_training_load", "hydration_ml",
    "hydration_goal_ml", "weight_kg", "bmi", "body_fat_pct",
    "body_water_pct", "muscle_mass_kg", "health_details",
)

_TREND_METRICS = (
    "steps", "resting_hr", "sleep_seconds", "sleep_score",
    "deep_sleep_seconds", "light_sleep_seconds", "rem_sleep_seconds",
    "awake_sleep_seconds", "nap_seconds",
    "hrv_last_night_avg", "stress_avg", "body_battery_charged",
    "heart_rate_min", "heart_rate_max", "respiration_awake_avg",
    "respiration_sleep_avg", "spo2_avg", "spo2_lowest", "spo2_sleep_avg",
    "intensity_moderate_minutes", "intensity_vigorous_minutes", "vo2_max",
    "fitness_age", "acute_training_load", "chronic_training_load",
    "hydration_ml", "weight_kg", "bmi", "body_fat_pct",
)

_FRESHNESS_LIMIT_HOURS = 36
_MAX_LATEST_DATA_AGE_DAYS = 2


def _text(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _row_to_dict(row) -> dict:
    d = dict(zip(_COLUMNS, row, strict=True))
    try:
        d["activities"] = json.loads(d["activities"] or "[]")
    except (TypeError, ValueError):
        pass
    try:
        d["health_details"] = json.loads(d["health_details"] or "{}")
    except (TypeError, ValueError):
        pass
    return d


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _today_local() -> date:
    from src.orchestrator import config

    try:
        return _now_utc().astimezone(ZoneInfo(str(config.TIMEZONE))).date()
    except (KeyError, ValueError):
        return _now_utc().astimezone().date()


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _freshness(conn, latest_date: str | None) -> dict:
    from src.orchestrator import config

    pipeline_enabled = bool(config.GARMIN_ENABLED)
    latest_synced_at = None
    if latest_date:
        row = conn.execute(
            "SELECT synced_at FROM garmin_days WHERE date = ?", (latest_date,)
        ).fetchone()
        latest_synced_at = row[0] if row else None
    state_row = conn.execute(
        "SELECT value FROM daemon_state WHERE key = 'last_garmin_sync'"
    ).fetchone()
    pipeline_at = state_row[0] if state_row else None
    pipeline_dt = _parse_utc_timestamp(pipeline_at)

    data_age_days = None
    if latest_date:
        try:
            data_age_days = (_today_local() - date.fromisoformat(latest_date)).days
        except (TypeError, ValueError):
            pass
    pipeline_age_hours = None
    if pipeline_dt is not None:
        pipeline_age_hours = round(
            (_now_utc() - pipeline_dt).total_seconds() / 3600,
            1,
        )

    reasons: list[str] = []
    if latest_date is None:
        reasons.append("no Garmin day is stored")
    elif data_age_days is None:
        reasons.append("latest Garmin date is invalid")
    elif data_age_days < 0:
        reasons.append("latest Garmin date is in the future")
    elif data_age_days > _MAX_LATEST_DATA_AGE_DAYS:
        reasons.append(
            f"latest Garmin day is {data_age_days} days old "
            f"(limit {_MAX_LATEST_DATA_AGE_DAYS})"
        )

    if not pipeline_enabled:
        reasons.append("scheduled Garmin sync is disabled")
    elif pipeline_at is None:
        reasons.append("no successful Garmin sync stamp is recorded")
    elif pipeline_dt is None:
        reasons.append("Garmin sync stamp is invalid")
    elif pipeline_age_hours is not None and pipeline_age_hours < -(5 / 60):
        reasons.append("Garmin sync stamp is in the future")
    elif (
        pipeline_age_hours is not None
        and pipeline_age_hours > _FRESHNESS_LIMIT_HOURS
    ):
        reasons.append(
            f"last successful Garmin sync is {pipeline_age_hours} hours old "
            f"(limit {_FRESHNESS_LIMIT_HOURS})"
        )

    return {
        "stale": bool(reasons),
        "reasons": reasons,
        "latest_data_date": latest_date,
        "latest_data_age_days": data_age_days,
        "latest_row_synced_at": latest_synced_at,
        "pipeline_last_success_at": pipeline_at,
        "pipeline_last_success_age_hours": pipeline_age_hours,
        "pipeline_enabled": pipeline_enabled,
        "freshness_limit_hours": _FRESHNESS_LIMIT_HOURS,
    }


@brain_tool(
    name="garmin_daily",
    description=(
        "One day of Garmin watch data (steps, resting HR, sleep including "
        "deep/light/REM/awake breakdown, naps, HRV, stress, body battery, "
        "activities, respiration, heart-rate range, intensity minutes, VO2 "
        "max, fitness age, training load, Pulse Ox, hydration and body "
        "composition). Defaults to the most recent day that has data. Null "
        "fields mean the watch recorded nothing — e.g. no sleep/HRV when it "
        "wasn't worn overnight, or no nap fields on a day with no nap — "
        "never zero. nap_start_local/nap_end_local cover the day's first "
        "nap start to last nap end; nap_seconds is the day's total. Every "
        "response includes freshness for both stored data and the sync job."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD; omit for the latest day with data."},
            "include_details": {"type": "boolean", "description": "Include lossless high-frequency/raw endpoint payloads (default false)."},
        },
    },
)
async def garmin_daily(args):
    conn = get_db()
    date = (args or {}).get("date")
    cols = ", ".join(_COLUMNS)
    latest_date = conn.execute("SELECT MAX(date) FROM garmin_days").fetchone()[0]
    freshness = _freshness(conn, latest_date)
    if date:
        row = conn.execute(
            f"SELECT {cols} FROM garmin_days WHERE date = ?",  # noqa: S608
            (date,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {cols} FROM garmin_days ORDER BY date DESC LIMIT 1"  # noqa: S608
        ).fetchone()
    if row is None:
        return _text({
            "no_data": True,
            "date": date,
            "freshness": freshness,
            "note": "No Garmin record for this day — the watch may not have "
                    "synced; do not treat as zero activity.",
        })
    out = _row_to_dict(row)
    if not bool((args or {}).get("include_details")):
        details = out.pop("health_details", {})
        out["detail_groups_available"] = sorted(details) if isinstance(details, dict) else []
    if not date:
        out["days_old"] = freshness["latest_data_age_days"]
        out["stale"] = freshness["stale"]
        if freshness["stale"]:
            out["note"] = (
                "Garmin freshness checks failed; do not present these as current: "
                + "; ".join(freshness["reasons"])
            )
    out["freshness"] = freshness
    return _text(out)


@brain_tool(
    name="garmin_trends",
    description=(
        "Aggregates over the last N days of Garmin data (window ends at the "
        "most recent synced day): avg/min/max per metric plus days_missing — "
        "days in the window where that metric was not recorded. Use for "
        "correlations like weight vs sleep vs steps. Includes sync freshness, and "
        "resting_hr_by_kind: overnight readings (nights with a sleep score) and "
        "daytime readings, which are different measurements."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Window size in days (default 14)."},
        },
    },
)
async def garmin_trends(args):
    try:
        days = int((args or {}).get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 365))
    conn = get_db()
    latest = conn.execute("SELECT MAX(date) FROM garmin_days").fetchone()[0]
    freshness = _freshness(conn, latest)
    if latest is None:
        return _text({
            "days_with_data": 0,
            "freshness": freshness,
            "note": "garmin_days is empty — the sync has not stored anything "
                    "yet.",
        })
    cols = ", ".join(_COLUMNS)
    rows = [
        _row_to_dict(r)
        for r in conn.execute(
            f"SELECT {cols} FROM garmin_days"  # noqa: S608
            " WHERE date > date(?, ?) AND date <= ?"
            " ORDER BY date",
            (latest, f"-{days} days", latest),
        ).fetchall()
    ]

    out: dict = {
        "window_days": days,
        "window_end": latest,
        "days_with_data": len(rows),
        "days_with_no_record": max(0, days - len(rows)),
        "freshness": freshness,
    }
    for metric in _TREND_METRICS:
        values = [r[metric] for r in rows if r[metric] is not None]
        out[metric] = {
            "avg": round(sum(values) / len(values)) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "days_missing": days - len(values),
        }
    out["resting_hr_by_kind"] = _resting_hr_by_kind(rows)
    return _text(out)


def _resting_hr_by_kind(rows: list[dict]) -> dict:
    """Resting heart rate split by how it was measured, which must never be compared across kinds.

    A day with a sleep score was worn overnight, so its resting heart rate is the
    overnight reading; a day without one only has a daytime reading.
    """
    kinds: dict[str, list[float]] = {"overnight": [], "daytime": []}
    for row in rows:
        if row["resting_hr"] is not None:
            kinds["overnight" if row["sleep_score"] is not None else "daytime"].append(row["resting_hr"])
    return {
        kind: {
            "days": len(values),
            "avg": round(sum(values) / len(values), 1) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
        for kind, values in kinds.items()
    }
