from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.brain.atomic_io import atomic_write_bytes, atomic_write_text
from src.brain.followups.morning import latest_night, needs_and_handled
from src.brain.followups.views import HomeData, home_view
from src.db import get_db, list_follow_ups

log = logging.getLogger("brain.followups.home")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHART_DAYS = 30
_OPEN_SHOWN = 5


def _followups_dir() -> Path:
    from src.brain.runtime.loader import resolve_user_dir

    return resolve_user_dir(_REPO_ROOT) / "followups"


def event_line(line: str) -> str | None:
    parts = line.split(" | ")
    if len(parts) < 3 or " → " not in parts[0]:
        return None
    start = parts[0].split(" → ")[0].strip()
    try:
        moment = datetime.fromisoformat(start)
    except ValueError:
        return None
    clock = moment.astimezone().strftime("%H:%M") if "T" in start else "all day"
    return f"{clock} {parts[2].strip()[1:-1]}"


def reminder_line(line: str, today: date) -> str | None:
    fields = {key.strip(): value.strip() for part in line.split(" | ") if "=" in part
              for key, value in [part.split("=", 1)]}
    due = fields.get("due", "")
    if not due or due == "—" or due[:10] > today.isoformat():
        return None
    title = fields.get("title", "").strip()
    return f"Reminder: {title[1:-1] if title.startswith('[') else title}"


def order_today(lines: list[str]) -> list[str]:
    def rank(line: str) -> tuple[int, str]:
        if line.startswith("all day"):
            return (0, line)
        return (2, line) if line.startswith("Reminder:") else (1, line)

    return sorted(lines, key=rank)


def _tool_text(result: dict) -> str:
    return "".join(block.get("text", "") for block in result.get("content", []))


def _today_lines(today: date) -> list[str]:
    from src.brain.mcp.tools.icloud_read import calendar_list_events, reminders_list

    lines: list[str] = []
    try:
        events = asyncio.run(calendar_list_events({
            "date_from": today.isoformat(), "date_to": (today + timedelta(days=1)).isoformat(),
        }))
        lines += [found for line in _tool_text(events).splitlines() if (found := event_line(line))]
    except Exception as exc:  # noqa: BLE001
        log.warning("home: calendar not read: %s", exc)
    try:
        reminders = asyncio.run(reminders_list({}))
        lines += [found for line in _tool_text(reminders).splitlines() if (found := reminder_line(line, today))]
    except Exception as exc:  # noqa: BLE001
        log.warning("home: reminders not read: %s", exc)
    return order_today(lines)


def _health(today: date) -> tuple[str, list[tuple[str, float]]]:
    conn = get_db()
    row = conn.execute(
        "SELECT date, weight_kg, sleep_score, hrv_last_night_avg, steps FROM garmin_days "
        "ORDER BY date DESC LIMIT 1"
    ).fetchone()
    weights = conn.execute(
        "SELECT date, weight_kg FROM garmin_days WHERE weight_kg IS NOT NULL AND date>=? ORDER BY date",
        ((today - timedelta(days=_CHART_DAYS)).isoformat(),),
    ).fetchall()
    if row is None:
        return "", []
    parts = [f"Garmin, {row[0]}"]
    for label, value, unit in (("weight", row[1], " kg"), ("sleep score", row[2], ""),
                               ("HRV", row[3], " ms"), ("steps", row[4], "")):
        if value is not None:
            parts.append(f"{label} {value:g}{unit}" if isinstance(value, float) else f"{label} {value}{unit}")
    return " · ".join(parts), [(str(day), float(kg)) for day, kg in weights]


async def _weight_file_id(notifier: Any, weights: list[tuple[str, float]], today: date) -> str | None:
    if len(weights) < 2:
        return None
    folder = _followups_dir()
    cache = folder / "home-chart.json"
    try:
        cached = json.loads(cache.read_text())
        if cached.get("date") == today.isoformat() and cached.get("file_id"):
            return str(cached["file_id"])
    except (OSError, ValueError):
        pass
    try:
        from src.brain.media.charts import render_chart

        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        png = await asyncio.to_thread(render_chart, {
            "title": "Weight, last 30 days", "kind": "line",
            "x": [day[5:] for day, _ in weights],
            "series": [{"name": "kg", "values": [kg for _, kg in weights]}],
        })
        path = folder / "home-weight.png"
        atomic_write_bytes(path, png, mode=0o600)
        file_id = await notifier.upload_private_file(path=path, title="Weight, last 30 days")
    except Exception as exc:  # noqa: BLE001
        log.warning("home: weight chart not made: %s", exc)
        return None
    if file_id:
        atomic_write_text(cache, json.dumps({"date": today.isoformat(), "file_id": file_id}))
    return file_id


async def build_home_view(notifier: Any, *, today: date | None = None) -> dict:
    today = today or datetime.now().date()
    night = latest_night(today)
    needs, handled = needs_and_handled(night, today) if night else ([], [])
    open_rows = [row for row in list_follow_ups() if row["status"] in ("open", "raised")]
    today_lines = await asyncio.to_thread(_today_lines, today)
    health_line, weights = await asyncio.to_thread(_health, today)
    return home_view(HomeData(
        needs_you=needs,
        today=today_lines,
        health_line=health_line,
        weight_file_id=await _weight_file_id(notifier, weights, today),
        open_loops=open_rows[:_OPEN_SHOWN],
        open_count=len(open_rows),
        handled=handled,
    ))


async def refresh_home(notifier: Any) -> None:
    owner = os.environ.get("OWNER_SLACK_USER_ID", "").strip()
    if not owner:
        return
    try:
        await notifier.publish_home(user=owner, view=await build_home_view(notifier))
    except Exception as exc:  # noqa: BLE001
        log.warning("home tab not refreshed: %s", exc)
