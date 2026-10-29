#!/bin/bash
# Create an encrypted restic backup of the operational databases, Markdown
# vault, and private user overlay. Databases are captured with SQLite's online
# backup command so live WAL databases are never copied directly.
#
# Operator prerequisites:
#   - install restic and sqlite3;
#   - initialize RESTIC_REPOSITORY;
#   - set RESTIC_PASSWORD_FILE to a readable, mode-0600 password file;
#   - keep both the repository and password recovery copy off this machine.
#
# The script resolves MEMORY_DB_PATH, VAULT_PATH, and USER_DIR through the
# repository's Python configuration, including its .env file. Override them in
# the environment when backing up a non-default deployment. See
# docs/RESTORE_DRILL.md before relying on this as a recovery path.

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_REPO_ROOT="${BRAIN_REPO_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
BRAIN_PYTHON="${BRAIN_PYTHON:-$BRAIN_REPO_ROOT/.venv/bin/python}"

if [ ! -x "$BRAIN_PYTHON" ]; then
  echo "Python runtime not found at $BRAIN_PYTHON; run make install first" >&2
  exit 1
fi

cd "$BRAIN_REPO_ROOT"

config_path() {
  "$BRAIN_PYTHON" -c "from src.orchestrator.config import $1; print($1)"
}

DB="${MEMORY_DB_PATH:-$(config_path MEMORY_DB_PATH)}"
HOUSING_DB="$(dirname -- "$DB")/housing.db"
VAULT_DIR="${VAULT_PATH:-$(config_path VAULT_PATH)}"
USER_OVERLAY="${USER_DIR:-$(config_path USER_DIR)}"
RESTIC_REPO="${RESTIC_REPOSITORY:-}"
PASS_FILE="${RESTIC_PASSWORD_FILE:-}"
SQLITE3_BIN="${SQLITE3_BIN:-$(command -v sqlite3 || true)}"
BACKUP_STAMP_PATH="${BRAIN_BACKUP_STAMP_PATH:-$(dirname -- "$DB")/backup-success.stamp}"

BACKUP_TMP_DIR=""
SNAPSHOT=""
HOUSING_SNAPSHOT=""

notify_failure() {
  if [ "${BRAIN_BACKUP_ALERT:-1}" = "0" ]; then
    return
  fi
  BACKUP_FAILURE_CODE="$1" "$BRAIN_PYTHON" -c '
import asyncio
import os
from src.brain.plugins.notifiers.slack import SlackNotifier
from src.orchestrator.config import BRIEF_CHANNEL_ID

if not BRIEF_CHANNEL_ID:
    raise SystemExit(1)
message = (
    "The nightly brain backup failed (exit "
    + os.environ.get("BACKUP_FAILURE_CODE", "unknown")
    + "). Check the private backup error log; the success stamp was not advanced."
)
result = asyncio.run(
    SlackNotifier(default_channel_id=BRIEF_CHANNEL_ID).send(
        message, title="brain backup failed"
    )
)
raise SystemExit(0 if result and result.get("status") == "sent" else 1)
' >/dev/null 2>&1 || true
}

on_exit() {
  status=$?
  trap - EXIT
  if [ -n "$SNAPSHOT" ]; then
    rm -f -- "$SNAPSHOT"
  fi
  if [ -n "$HOUSING_SNAPSHOT" ]; then
    rm -f -- "$HOUSING_SNAPSHOT"
  fi
  if [ -n "$BACKUP_TMP_DIR" ]; then
    rmdir -- "$BACKUP_TMP_DIR" 2>/dev/null || true
  fi
  if [ "$status" -ne 0 ]; then
    notify_failure "$status"
  fi
  exit "$status"
}
trap on_exit EXIT

file_uid() {
  stat -f '%u' "$1" 2>/dev/null || stat -c '%u' "$1"
}

file_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"
}

require_private_file() {
  path="$1"
  label="$2"
  if [ -L "$path" ] || [ ! -f "$path" ]; then
    echo "$label must be a real regular file, not a symlink: $path" >&2
    exit 1
  fi
  if [ "$(file_uid "$path")" != "$(id -u)" ] || [ "$(file_mode "$path")" != "600" ]; then
    echo "$label must be owned by the current user with mode 0600: $path" >&2
    exit 1
  fi
}

require_private_directory() {
  path="$1"
  label="$2"
  if [ -L "$path" ] || [ ! -d "$path" ]; then
    echo "$label must be a real directory, not a symlink: $path" >&2
    exit 1
  fi
  if [ "$(file_uid "$path")" != "$(id -u)" ] || [ "$(file_mode "$path")" != "700" ]; then
    echo "$label must be owned by the current user with mode 0700: $path" >&2
    exit 1
  fi
}

if [ -z "$RESTIC_REPO" ]; then
  echo "RESTIC_REPOSITORY must name an initialized off-machine repository" >&2
  exit 1
fi
if [ -z "$PASS_FILE" ] || [ ! -r "$PASS_FILE" ]; then
  echo "RESTIC_PASSWORD_FILE must name a readable regular file, not a symlink" >&2
  exit 1
fi
require_private_file "$PASS_FILE" "RESTIC_PASSWORD_FILE"
if [ -z "$SQLITE3_BIN" ] || [ ! -x "$SQLITE3_BIN" ]; then
  echo "sqlite3 is not installed or SQLITE3_BIN is not executable" >&2
  exit 1
fi
if ! command -v restic >/dev/null 2>&1; then
  echo "restic is not installed" >&2
  exit 1
fi
require_private_directory "$(dirname -- "$DB")" "memory DB directory"
require_private_file "$DB" "memory DB"

if [ -L "$HOUSING_DB" ]; then
  echo "housing DB must not be a symlink: $HOUSING_DB" >&2
  exit 1
elif [ -e "$HOUSING_DB" ]; then
  require_private_file "$HOUSING_DB" "housing DB"
elif [ "${BRAIN_BACKUP_REQUIRE_HOUSING_DB:-0}" = "1" ]; then
  echo "required housing DB not found at $HOUSING_DB" >&2
  exit 1
fi

if [ "${BRAIN_BACKUP_SKIP_VAULT:-0}" != "1" ]; then
  require_private_directory "$VAULT_DIR" "vault directory"
fi
if [ "${BRAIN_BACKUP_SKIP_USER:-0}" != "1" ]; then
  require_private_directory "$USER_OVERLAY" "private user overlay"
fi

export RESTIC_PASSWORD_FILE="$PASS_FILE"
if ! restic -r "$RESTIC_REPO" cat config >/dev/null 2>&1; then
  echo "restic repository is unreachable, locked, or not initialized: $RESTIC_REPO" >&2
  exit 1
fi

BACKUP_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/brain-backup.XXXXXX")"
chmod 700 "$BACKUP_TMP_DIR"
SNAPSHOT="$BACKUP_TMP_DIR/memory-backup.db"
HOUSING_SNAPSHOT="$BACKUP_TMP_DIR/housing-backup.db"

# 1. Consistent DB snapshots (work alongside live WAL writers).
"$SQLITE3_BIN" "$DB" ".backup $SNAPSHOT"
chmod 600 "$SNAPSHOT"
MEMORY_CHECK="$("$SQLITE3_BIN" "$SNAPSHOT" "PRAGMA quick_check;")"
if [ "$MEMORY_CHECK" != "ok" ]; then
  echo "memory snapshot failed SQLite quick_check; refusing upload" >&2
  exit 1
fi

TARGETS=("$SNAPSHOT")
if [ -e "$HOUSING_DB" ]; then
  "$SQLITE3_BIN" "$HOUSING_DB" ".backup $HOUSING_SNAPSHOT"
  chmod 600 "$HOUSING_SNAPSHOT"
  HOUSING_CHECK="$("$SQLITE3_BIN" "$HOUSING_SNAPSHOT" "PRAGMA quick_check;")"
  if [ "$HOUSING_CHECK" != "ok" ]; then
    echo "housing snapshot failed SQLite quick_check; refusing upload" >&2
    exit 1
  fi
  TARGETS+=("$HOUSING_SNAPSHOT")
fi

# 2. Encrypted backup. Vault and private overlay are required unless the
# operator explicitly opts out; preflight above rejects missing/symlink roots.
if [ "${BRAIN_BACKUP_SKIP_VAULT:-0}" != "1" ]; then
  TARGETS+=("$VAULT_DIR")
fi
if [ "${BRAIN_BACKUP_SKIP_USER:-0}" != "1" ]; then
  TARGETS+=("$USER_OVERLAY")
fi

restic -r "$RESTIC_REPO" backup --tag brain-nightly "${TARGETS[@]}"
restic -r "$RESTIC_REPO" forget --tag brain-nightly \
  --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune

BACKUP_COMPLETED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
"$BRAIN_PYTHON" -c '
import sys
from pathlib import Path
from src.brain.atomic_io import atomic_write_text

atomic_write_text(
    Path(sys.argv[1]),
    sys.argv[2] + "\n",
    mode=0o600,
    preserve_existing_mode=False,
)
' "$BACKUP_STAMP_PATH" "$BACKUP_COMPLETED_AT"

echo "backup complete: $BACKUP_COMPLETED_AT"
