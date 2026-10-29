from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import stat
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.brain.atomic_io import ensure_real_directory
from src.orchestrator._tasks import spawn_tracked
from src.orchestrator.config import TIMEZONE as TZ
from src.orchestrator.config import USER_DIR

_LOG_FILE = Path(
    os.getenv("BRAIN_LOG_PATH", str(USER_DIR / "logs" / "brain-agents.log"))
).expanduser()


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600, follow_symlinks=False)
        return stream


def _private_log_handler(path: Path) -> logging.StreamHandler:
    parent = ensure_real_directory(path.parent)
    parent_info = parent.lstat()
    if hasattr(os, "getuid") and parent_info.st_uid != os.getuid():
        raise PermissionError(f"daemon log directory is not owned by this user: {parent}")
    if stat.S_IMODE(parent_info.st_mode) & 0o077:
        raise PermissionError(
            f"daemon log directory must already be private (mode 0700): {parent}"
        )
    path = parent / path.name
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"daemon log path is not a regular file: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError(f"daemon log is not owned by this user: {path}")

    return _PrivateRotatingFileHandler(
        path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

log = logging.getLogger("brain-scheduler")

GARMIN_MISFIRE_GRACE_SECS = 21600


def _configure_logging(path: Path = _LOG_FILE) -> None:
    file_handler = _private_log_handler(path)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler],
        force=True,
    )


def _set_daemon_state(key: str, value: str) -> None:
    from src.db import get_db

    conn = get_db()
    conn.execute(
        "INSERT INTO daemon_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def _log_job_event(event) -> None:
    if event.code == EVENT_JOB_ERROR:
        log.error("APScheduler job %s raised: %s",
                  event.job_id, event.exception, exc_info=event.exception)
    elif event.code == EVENT_JOB_MISSED:
        log.warning("APScheduler job %s missed (scheduled %s)",
                    event.job_id, event.scheduled_run_time)


async def _run_public_discovery(config) -> None:
    from src.db import utcnow
    from src.orchestrator import public_discovery

    try:
        counts = await public_discovery.run_once(config)
    except Exception as e:  # noqa: BLE001
        log.warning(f"public discovery collection failed: {e}")
        return
    if counts.get("candidates"):
        log.info(f"public discovery collected: {counts}")
    try:
        _set_daemon_state("last_public_discovery", utcnow())
    except Exception as e:  # noqa: BLE001
        log.warning(f"failed to stamp last_public_discovery: {e}")


async def _run_local_backup() -> None:
    from src.orchestrator.config import MEMORY_DB_PATH, VAULT_PATH
    from src.orchestrator.local_backup import BackupError, run_backup

    try:
        result = await asyncio.to_thread(run_backup, vault=Path(VAULT_PATH), database=Path(MEMORY_DB_PATH))
    except BackupError as exc:
        log.error("local backup failed: %s", exc)
        return
    log.info("local backup: %s", result)


async def _run_due_reminders() -> None:
    from src.orchestrator.reminders import give_due_reminders

    try:
        await give_due_reminders()
    except Exception as exc:  # noqa: BLE001
        log.error("smart reminders failed: %s", exc)


async def _maybe_catch_up_backup() -> None:
    from src.orchestrator.config import MEMORY_DB_PATH
    from src.orchestrator.local_backup import stamp_path

    stamp = stamp_path(Path(MEMORY_DB_PATH))
    try:
        last = datetime.fromisoformat(stamp.read_text().strip())
    except (OSError, ValueError):
        last = None
    if last is None or (datetime.now(UTC) - last).total_seconds() > 24 * 3600:
        log.info("last local backup is missing or over a day old; running it now")
        await _run_local_backup()


async def _run_garmin_sync(days_back: int = 7, include_today: bool = False) -> None:
    from src.db import utcnow
    from src.orchestrator import garmin_sync

    label = "garmin sync"
    try:
        counts = await asyncio.to_thread(
            garmin_sync.run_once, None, days_back, None, include_today
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"{label} failed: {e}")
        return
    log.info(f"{label}: {counts}")
    try:
        _set_daemon_state("last_garmin_sync", utcnow())
    except Exception as e:  # noqa: BLE001
        log.warning(f"failed to stamp last_garmin_sync: {e}")


async def _run_backup_freshness_check() -> None:
    from src.brain.doctor import Check
    from src.brain.doctor.checks import check_backup_freshness
    from src.brain.doctor.notify import send_failure_ping

    repo_root = Path(__file__).resolve().parent.parent.parent
    try:
        issues = check_backup_freshness(None, repo_root)
    except Exception as exc:  # noqa: BLE001
        from src.brain.doctor import Issue

        issues = [Issue(
            file="<backup-monitor>",
            message=f"backup freshness check crashed: {type(exc).__name__}: {exc}",
        )]
    if not issues:
        log.info("backup freshness monitor: healthy")
        return

    for issue in issues:
        log.warning("backup freshness monitor: %s: %s", issue.file, issue.message)
    check = Check(
        name="check_backup_freshness",
        severity="warn",
        fn=check_backup_freshness,
    )
    try:
        await send_failure_ping([(check, issues)])
    except Exception as exc:  # noqa: BLE001
        log.warning("backup freshness alert failed: %s", exc)


async def _run_mode(mode: str):
    log.info(f"Job triggered: {mode}")
    if mode == "all_morning":
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from src.db import get_db
            today_str = datetime.now(ZoneInfo(str(TZ))).strftime("%Y-%m-%d")
            conn = get_db()
            conn.execute(
                "INSERT INTO daemon_state(key, value) VALUES('last_morning_trigger', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (today_str,),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to stamp last_morning_trigger: {e}")
    try:
        from src.orchestrator.runner import run_orchestrated
        await run_orchestrated(mode)
        log.info(f"Job completed: {mode}")
    except Exception as e:
        log.error(f"Job failed: {mode} — {e}", exc_info=True)


def create_scheduler() -> AsyncIOScheduler:
    from src.orchestrator.registry import MODE_SPECS

    _DISPLAY_NAMES = {
        "all_morning": "Full Cycle AM",
        "scout": "Scout (discovery)",
    }

    scheduler = AsyncIOScheduler(timezone=TZ)
    # Surface APScheduler-internal failures (job-error escapes _run_mode,
    # misfires beyond the grace window). Without a listener these vanish
    # — log says "Job triggered" but no completion or error follows.
    scheduler.add_listener(_log_job_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    from src.orchestrator.config import agents_paused

    paused = agents_paused()
    if paused:
        log.info("BRAIN_AGENTS_PAUSED is set — no agent mode is scheduled.")
    for mode_name, spec in MODE_SPECS.items():
        if paused or not spec.schedule:
            continue
        display = _DISPLAY_NAMES.get(mode_name, mode_name)
        scheduler.add_job(
            _run_mode,
            CronTrigger.from_crontab(spec.schedule, timezone=TZ),
            args=[mode_name],
            id=mode_name,
            name=display,
            max_instances=1,
            coalesce=True,
            # If the Mac was asleep when the job was due, fire as soon as we
            # wake up — up to 6 hours late. Without this, APScheduler silently
            # skips misfired jobs and a sleeping laptop means no morning run.
            misfire_grace_time=spec.misfire_grace_seconds or 21600,
        )

    from src.orchestrator.config import GARMIN_ENABLED, REPO_ROOT
    from src.orchestrator.public_discovery import load_config as load_public_discovery

    public_discovery = load_public_discovery(REPO_ROOT)
    if public_discovery.enabled:
        scheduler.add_job(
            _run_public_discovery,
            CronTrigger.from_crontab(public_discovery.schedule, timezone=TZ),
            args=[public_discovery],
            id="public_discovery",
            name="Public discovery collector",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    else:
        log.info("Public discovery collector disabled")

    if os.getenv("BRAIN_BACKUP_EXPECTED", "").strip() == "1":
        scheduler.add_job(
            _run_backup_freshness_check,
            CronTrigger.from_crontab("15 9 * * *", timezone=TZ),
            id="backup_freshness",
            name="Backup freshness monitor",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=21600,
        )

    scheduler.add_job(
        _run_due_reminders,
        CronTrigger.from_crontab("*/5 * * * *", timezone=TZ),
        id="smart_reminders",
        name="Smart reminders",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _run_local_backup,
        CronTrigger.from_crontab("35 23 * * *", timezone=TZ),
        id="local_backup",
        name="Local backup (vault history and database copy)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=21600,
    )

    if not GARMIN_ENABLED:
        log.info("Garmin sync disabled (set GARMIN_ENABLED=true to enable)")
        return scheduler

    scheduler.add_job(
        _run_garmin_sync,
        CronTrigger.from_crontab("0 7 * * *", timezone=TZ),
        kwargs={"days_back": 7, "include_today": True},
        id="garmin_sync",
        name="Garmin sync (daily)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=GARMIN_MISFIRE_GRACE_SECS,
    )

    return scheduler


async def _maybe_catch_up_morning() -> None:
    from src.orchestrator.config import agents_paused

    if agents_paused():
        log.info("BRAIN_AGENTS_PAUSED is set — skipping the morning catch-up.")
        return

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.db import get_db
    from src.orchestrator.registry import MODE_SPECS

    morning_spec = MODE_SPECS.get("all_morning")
    morning_hour = 5
    if morning_spec and morning_spec.schedule:
        try:
            hour_field = morning_spec.schedule.split()[1]
            if hour_field != "*":
                morning_hour = min(int(h) for h in hour_field.replace("-", ",").split(","))
        except (ValueError, IndexError) as e:
            log.warning(
                "could not parse morning schedule %r, defaulting to hour %d: %s",
                morning_spec.schedule, morning_hour, e,
            )

    tz = ZoneInfo(str(TZ))
    now_local = datetime.now(tz)
    today_str = now_local.strftime("%Y-%m-%d")
    if now_local.hour < morning_hour:
        return

    conn = get_db()
    row = conn.execute(
        "SELECT value FROM daemon_state WHERE key='last_morning_trigger'"
    ).fetchone()
    if row and row[0] == today_str:
        log.info(f"Morning run already triggered today ({today_str}); skipping catch-up.")
        return

    log.info(f"Morning run not yet triggered today ({today_str}); firing catch-up.")
    conn.execute(
        "INSERT INTO daemon_state(key, value) VALUES('last_morning_trigger', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (today_str,),
    )
    conn.commit()
    spawn_tracked(_run_mode("all_morning"), "morning catch-up", log)


async def _start_listeners() -> None:
    from src.brain.actions.executor import run_forever as _run_executor
    from src.brain.mcp._common import integrations_config
    from src.brain.plugins.loader import load_listener

    spawn_tracked(_run_executor(), "action executor", log)
    log.info("action executor daemon started")

    listener = load_listener(integrations_config())
    if listener is None:
        log.warning("No reply listener configured — inbound replies disabled.")
        return

    from src.orchestrator.reply_routing import on_reply

    spawn_tracked(listener.run(on_reply), "reply listener", log)
    log.info(f"Reply listener started: {type(listener).__name__}")


async def _daemon(list_only: bool = False):
    import os as _os
    if _os.environ.get("BRAIN_SKIP_DOCTOR") != "1":
        from src.brain.doctor import issue_level, run_checks
        log.info("Running brain doctor checks before scheduler start...")
        repo_root = Path(__file__).resolve().parent.parent.parent
        results, exit_code = run_checks(repo_root, strict=False)
        for check, issues in results:
            if not issues:
                continue
            for issue in issues:
                severity = issue_level(check, issue)
                if severity == "info":
                    continue
                loc = f"{issue.file}:{issue.line}" if issue.line else issue.file
                log.warning(f"doctor {severity}: {check.name} — {loc}: {issue.message}")
        if any(issue_level(check, issue) != "info" for check, issues in results for issue in issues):
            try:
                from src.brain.doctor.notify import send_failure_ping
                await send_failure_ping(results)
            except Exception as e:  # noqa: BLE001
                log.warning(f"doctor failure ping failed: {e}")
        if exit_code != 0:
            log.error("doctor reported errors — refusing to start scheduler. "
                      "Set BRAIN_SKIP_DOCTOR=1 to bypass for an emergency boot.")
            sys.exit(1)
        log.info("doctor: PASS")
    else:
        log.warning("BRAIN_SKIP_DOCTOR=1 — skipping doctor checks at boot.")

    from src.db import utcnow

    _set_daemon_state("daemon_started_at", utcnow())

    scheduler = create_scheduler()
    scheduler.start()

    for job in scheduler.get_jobs():
        log.info(f"  Scheduled: {job.id} — {job.name}")

    if list_only:
        scheduler.shutdown(wait=False)
        return

    log.info("Daemon running. Waiting for scheduled jobs...")

    try:
        await _maybe_catch_up_morning()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Missed-run check failed: {e}")

    try:
        await _maybe_catch_up_backup()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Backup catch-up failed: {e}")

    try:
        await _start_listeners()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Reply listener not started: {e}.")

    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log.info("Shutting down...")
        scheduler.shutdown(wait=False)
        log.info("Daemon stopped.")


def main():
    parser = argparse.ArgumentParser(description="Brain Agents Scheduler Daemon")
    parser.add_argument("--list", action="store_true", help="List all scheduled jobs")
    args = parser.parse_args()

    _configure_logging()
    log.info("=" * 50)
    log.info("Starting brain-agents scheduler daemon")
    log.info(f"Timezone: {TZ}")
    log.info("=" * 50)

    asyncio.run(_daemon(list_only=args.list))


if __name__ == "__main__":
    main()
