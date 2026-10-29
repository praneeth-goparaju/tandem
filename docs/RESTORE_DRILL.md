# Backup and restore drill

## Status

This repository ships backup code and deployment templates, not a recovery
guarantee. Recovery remains **unproven for each installation** until an operator
has completed this drill from the actual off-machine restic repository, checked
the restored SQLite database, started the daemon, and recorded the result below.

`make install` does not configure restic, install launchd jobs, copy secrets, or
perform this drill.

## Nightly local backup

The daemon also backs up every night at 23:35, and once at start-up when the
last backup is over a day old (`src/orchestrator/local_backup.py`). It commits
the vault into a separate Git directory (`BRAIN_VAULT_HISTORY`, default
`~/brain-vault.git`; create it once with `git init --bare ~/brain-vault.git`,
or the backup fails every night and the stamp is never written) and copies the database with SQLite's backup API into
`<database folder>/backups/`, keeping 7 days. It writes the success stamp that
`check_backup_freshness` reads only when both steps worked. Both copies stay on
this Mac: they recover from a bad edit or a deleted note, not from losing the
Mac. To restore a note, run `git --git-dir ~/brain-vault.git log -- "<note>"`
and `git --git-dir ~/brain-vault.git show <commit>:"<note>"`.

## Recovery boundary

`scripts/backup_memory.sh` creates one restic snapshot containing:

- a temporary `memory-backup.db` produced with SQLite's online `.backup` API;
- a temporary `housing-backup.db` snapshot when the sibling live `housing.db`
  exists (saved watches, listing history, and alert deduplication);
- the configured Markdown vault;
- the configured private `USER_DIR` overlay.

The vault and private overlay are required by default. A deliberately
database-only deployment must opt out with `BRAIN_BACKUP_SKIP_VAULT=1` and/or
`BRAIN_BACKUP_SKIP_USER=1`; a missing path never silently produces a green
backup. Set `BRAIN_BACKUP_REQUIRE_HOUSING_DB=1` when housing state is part of
the installation's recovery boundary.

The script deliberately does **not** back up the Git checkout, `.venv`,
`.env`, service-account JSON, the Claude Code login, macOS privacy
grants, or the restic password. Recreate code and dependencies from Git; recover
secrets and credentials from a separate password manager.

Restic preserves the source paths in the snapshot. Because repository, vault,
database, temp, and overlay paths vary by installation, inspect `restic ls`
instead of assuming a fixed restored path.

## One-time operator setup

1. Install `restic` and `sqlite3`.
2. Choose a `RESTIC_REPOSITORY` that survives loss of this computer. A
   repository stored only on the same disk is not disaster recovery.
3. Create a strong restic password file with mode `0600`, store a recovery copy
   separately from the repository, and set `RESTIC_PASSWORD_FILE` to its local
   path.
   The database files and password file must be owned by the current user with
   mode `0600`; the database parent, vault, and private overlay roots must be
   real, non-symlink directories owned by that user with mode `0700`.
4. Initialize the repository explicitly:

   ```bash
   export RESTIC_REPOSITORY="<off-machine restic repository>"
   export RESTIC_PASSWORD_FILE="<absolute path to password file>"
   restic -r "$RESTIC_REPOSITORY" init
   ```

5. Run the backup manually from the repository root:

   ```bash
   bash scripts/backup_memory.sh
   restic -r "$RESTIC_REPOSITORY" snapshots --tag brain-nightly
   restic -r "$RESTIC_REPOSITORY" check
   ```

   A fully successful backup and retention pass atomically writes a private
   `backup-success.stamp` beside `MEMORY_DB_PATH`. Set
   `BRAIN_BACKUP_EXPECTED=1` in the daemon's `.env` so brain doctor monitors
   that stamp; `BRAIN_BACKUP_STAMP_PATH` overrides its location for both
   processes. The daemon rechecks it daily at 09:15 and alerts when the last
   success is more than 36 hours old. Backup failures leave the previous stamp
   untouched and make an immediate best-effort Slack alert to
   `BRIEF_CHANNEL_ID` (disable only with `BRAIN_BACKUP_ALERT=0`).

6. Render `ops/com.example.tandem.backup.plist.example` into a local
   plist. Replace every `__PLACEHOLDER__` with an absolute path; launchd does not
   expand shell variables or `~`. Create the selected log directory and validate
   the rendered file with `plutil -lint` before installing it under
   `~/Library/LaunchAgents/`.

The backup script fails closed if required roots are missing/unsafe, the
repository is not initialized/reachable, or the password file is unavailable.
It does not silently create a same-disk repository or generate the only
password copy on the machine being protected.

## Restore drill

Perform this in a disposable restore directory first. Do not copy over live
state until inspection succeeds.

1. **Prepare a clean checkout.** Clone the repository to the intended absolute
   path, install Python 3.12, then run `make install` (add the garmin extra if the overlay enables it).
2. **Recover restic access.** Restore the password file from the password
   manager, set mode `0600`, and export `RESTIC_REPOSITORY` and
   `RESTIC_PASSWORD_FILE`.
3. **Inspect before restoring.** Run:

   ```bash
   restic -r "$RESTIC_REPOSITORY" snapshots --tag brain-nightly
   BRAIN_SNAPSHOT_ID="$(restic -r "$RESTIC_REPOSITORY" snapshots \
     --tag brain-nightly --latest 1 --json | python3 -c \
     'import json,sys; rows=json.load(sys.stdin); print(rows[-1]["id"] if rows else "")')"
   test -n "$BRAIN_SNAPSHOT_ID"
   restic -r "$RESTIC_REPOSITORY" ls "$BRAIN_SNAPSHOT_ID"
   restic -r "$RESTIC_REPOSITORY" check
   ```

4. **Restore into an empty staging directory.** Set `RESTORE_ROOT` to a new,
   dedicated directory and run:

   ```bash
   restic -r "$RESTIC_REPOSITORY" restore "$BRAIN_SNAPSHOT_ID" --target "$RESTORE_ROOT"
   find "$RESTORE_ROOT" -type f \
     \( -name memory-backup.db -o -name housing-backup.db \) -print
   ```

5. **Verify SQLite.** Set `DB_SNAPSHOT` to the single restored
   `memory-backup.db` path shown above, then require both commands to succeed:

   ```bash
   sqlite3 "file:${DB_SNAPSHOT}?mode=ro" "PRAGMA quick_check;"
   sqlite3 "file:${DB_SNAPSHOT}?mode=ro" \
     "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
   ```

   `quick_check` must print `ok`. Multiple or missing database snapshots require
   operator investigation; do not guess which file is current.

   When `housing-backup.db` is present, set `HOUSING_SNAPSHOT` to its single
   restored path and require the same checks:

   ```bash
   sqlite3 "file:${HOUSING_SNAPSHOT}?mode=ro" "PRAGMA quick_check;"
   sqlite3 "file:${HOUSING_SNAPSHOT}?mode=ro" \
     "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
   ```

6. **Stop writers.** If a daemon is already installed, stop it with
   `launchctl bootout` before replacing the database, overlay, or vault.
7. **Map and copy state deliberately.** Use `restic ls "$BRAIN_SNAPSHOT_ID"` to identify the
   restored source paths. Copy the verified DB to the deployment's configured
   `MEMORY_DB_PATH` with mode `0600`; copy the verified housing snapshot beside
   it as `housing.db`, also mode `0600`. Restore the `USER_DIR` overlay with
   private directory permissions. Restore the vault only after comparing it
   with any cloud-synced/current copy; do not blindly overwrite a newer vault.
8. **Restore secrets separately.** Recreate `.env`, service-account files, the Claude Code
   login (run `claude` once), and other credentials from their independent recovery stores. Regrant
   macOS Reminders permission to the exact Python executable used by the daemon.
9. **Validate before daemonizing.** From the restored checkout run:

   ```bash
   .venv/bin/python -m src.cli doctor --strict
   .venv/bin/ruff check .
   .venv/bin/python -m pytest tests -q
   .venv/bin/python tools/claude_smoke.py --confirm
   ```

10. **Render and install the daemon.** Replace every placeholder in
    `ops/com.example.tandem.daemon.plist.example`, create its log
    directory, run `plutil -lint`, install the rendered plist under
    `~/Library/LaunchAgents/`, and start it with `launchctl bootstrap`.
11. **Smoke test with operator intent.** Confirm scheduled jobs appear in logs.
    If Slack is configured, send a deliberately initiated test message and
    verify a reply round trip. Do not run live agents as part of automated
    tests.

## Drill log

Do not mark a drill successful unless it used the real off-machine recovery
materials and reached the daemon smoke test.

| Date | Operator | Snapshot | `quick_check` | Daemon smoke | Elapsed | Notes |
|---|---|---|---|---|---|---|
| pending | — | — | — | — | — | No completed operator drill recorded. |
