# Database

Operational state lives in one local SQLite database addressed by
`config.MEMORY_DB_PATH`. The default path remains `search/data/vault.db` for
compatibility with existing installations.

`VAULT_DB_PATH` is accepted as a legacy environment fallback and Python alias;
it does not identify a second database.

## Access

Use `src.db.get_db()`. It returns one connection per thread, enables WAL and a
busy timeout, initializes the schema, and enforces private directory/file
permissions. Tests must use the isolated database fixture rather than importing
a connection before environment overrides are applied.

Do not open the live database from model tools or log row payloads containing
personal data.

## Core tables

### `findings`

The shared agent observation ledger. Important columns include agent, category,
title/detail/action, source URL, confidence, reasoning, expiry, acknowledgement,
and a unique normalized content hash.

`store_finding` inserts rows into this ledger with content-hash deduplication.

`from_outside` (migration 16, `alfred-stage-1`) is 1 when the run that stored
the finding had received outside content: mail, calendar, contacts, the web,
intake or listing results, an image or an attached audio file, or a finding
already flagged this way. It
is 0 otherwise, and for every row that predates the migration.
`get_recent_findings` tells the runner when it returns a flagged finding: any
run that read one flags what it stores next, and an unattended run also loses
direct vault edits for the rest of the run.

### `pending_actions`

Durable action state machine and approval boundary.

Key fields:

- `type`, canonical `payload_json`, and `payload_hash`;
- `dedupe_key`, protected by a partial unique index while active, plus an
  optional immutable `occurrence_key` for intentional repeated business events;
- `risk`, `status`, retry count, timestamps, and execution result;
- immutable `origin_agent` and `origin_area`;
- `approval_audience`, channel/signal, actor, and action `version`;
- source finding IDs and human-facing rationale.

Active statuses are `pending`, `approved`, and `executing`. Terminal states are
`executed`, `partial`, `ambiguous`, `failed`, `rejected`, and `expired`.
`partial` means at least one effect completed and whole-action replay is unsafe;
`ambiguous` means a deadline or crash left the external outcome unknown.
Approval and execution use guarded transitions; callers must not update status
directly.

Current human approval provenance uses `slack_reply` for typed Slack replies
and `slack_button` for Block Kit decisions. The executor accepts the historical
`signal_reply` value only so already-approved legacy rows can finish draining.

`send_draft` proposals snapshot the confined draft's SHA-256, byte size, kind,
recipient, title, and body preview into the immutable payload. Approval cards
show this frozen effect, and the handler refuses delivery if the file no longer
matches. A successful local status rewrite records the approved digest so a
post-commit retry remains an idempotent no-op.

### `action_idempotency_receipts`

An atomic reservation/receipt for each action fingerprint. Active reservations
do not expire. Execution-terminal receipts are retained for seven days, so
an auto-action whose successful response was lost cannot be proposed and run a
second time. Rejected and expired actions release their reservation because no
effect ran. Receipt creation and status changes are database triggers in the
same transaction as the action row. Expired receipts are pruned on the next
queue insertion.

To intentionally repeat an otherwise identical payload, supply a stable
`occurrence_key` such as `daily-brief:2026-08-23`. Retries of one occurrence
must reuse its key; a new business occurrence uses a different key.

### `agent_runs`

One terminal row per Claude run. It records agent, provider/model, start/end,
status/error, duration, findings count, input/output/cache tokens, and cost.
There is no second transcript table and agents cannot self-log runs.

### `notifications`

Outbound Slack audit/correlation records: agent, title/message, status, HTTP
status, finding ID, Slack timestamp, area/channel, and creation time. The
historical `signal_timestamp` name stores the transport message identifier.

### `knowledge`

A frozen read-only archive retained for recall. New durable knowledge goes
through confirmed vault promotion rather than inserts into this table.

### `intake`

Quarantined external items and public-discovery candidates. Rows with
`source='mission-control'` come from a check-in source that no longer exists;
they stay readable and nothing writes new ones.
`source_id` is unique; content and metadata remain untrusted until dismissal or
confirmed promotion. The public collector writes `source='public-discovery'`,
`kind='web_candidate'`; Sherlock's narrow reader cannot access other intake
sources and marks assessed candidates dismissed without promoting raw content.

### `briefs`

Retired. The table holds old briefs (date, composed time, JSON sections,
mirror time and source run ID); nothing writes or reads it any more.

### `events`

Append-only audit journal. It stores actor/action/subject/trust, detail, and a
hash of the triggering payload rather than the payload itself.

Direct edits, the run guard, helpers and chat add five actions: `direct_edit` and
`edit_refused` (subject: the vault path; the reason for a refusal is in
`detail`), `tool_refused` (subject: the tool; any other call the run guard
refuses, with the reason in `detail`), `helper_start` (subject: the run label), and `co_owner_session`
(actor: the chat agent, subject: the place) for every session a sender other
than the owner starts.

Media adds two more, both with actor `media:<agent>` and the media id as
subject: `media_rendered` when `draw_chart` or `draw_image` saves a picture
(`detail`: kind, size in bytes and place) and `media_sent` when the Slack
notifier posts it (`detail`: channel, place and size).

### `vault_edits`

The ledger of direct vault edits (migration 16, `alfred-stage-1`): one row for
each `Edit` or `Write` that `run_guard.py` allowed and Claude Code completed.
Columns are `ts` (UTC), `agent`, `run_label` (`<agent>@<start time>`), `tool`
(`Edit` or `Write`), `path` (relative to the vault), and `sha_before` and
`sha_after`, the SHA-256 of the file's bytes before and after the call.
`sha_before` is null for a file the call created. The runner's `PostToolUse`
hook writes the row; `list_vault_edits` reads it, limited to the caller's
areas in a chat. The table holds checksums, not content, so undoing an edit
means restoring the file from the vault's backup.

### `follow_ups`

The follow-up ledger (migration 17, `alfred-stage-2-3`): what was asked,
promised or left open, found by the follow-up run. `id` is `F-` and six digits,
in the order of creation. `source` is `slack_shared`, `slack_private`,
`slack_old`, `claude_code` or `claude_ai`; `ref` points back to the message (a
Slack link, or the folder, file and line of a Claude Code transcript);
`asked_by` is who asked; `place` is `shared` for `slack_shared` and `private`
otherwise, and both come from the collector's index of the digest, never from
the run; `summary` is the run's own sentence on what is open, never the
message itself. `status` is `open`, `raised`, `done`, `dismissed` or `expired`.
`first_seen` and `last_raised` are UTC `YYYY-MM-DDTHH:MM:SS`, and `last_raised`
is when the follow-up run last raised the item for a morning message, at most
five items a day. `research` holds what the run found out. `follow_ups_add`
returns the known id for a `source` and `ref` that already have a row, so a
message collected twice is one item, and adding it again does not reopen a
closed item. `dedupe_key` is unique: a hash of the source, the reference and the
start of the summary. The collector expires `open` and `raised` rows first seen
more than 14 days ago. The tools in `src/brain/mcp/tools/follow_ups.py` and the
helpers in `src/db.py` read and write it.
### `media`

The pictures that `draw_chart` and `draw_image` render (migration 17,
`alfred-stage-2-3`). Each picture is a PNG at `$USER_DIR/media/<id>.png` (the
folder mode `0700`, the file `0600`), and `store.save_png` writes its row in
the same call. Columns:

- `id`: `m-` and 12 lowercase hex characters. A run only ever holds the id;
  tools take and return no paths.
- `ts` (UTC), `agent`, `run_label` (`<agent>@<start time>`) and `area` of the run
  that drew it.
- `place`: `shared` or `private`. An id can be posted only in the place it was
  made for.
- `kind` (`chart` or `image`), `path` (`media/<id>.png`, relative to the private
  overlay), `bytes` and `sha256` of the PNG.
- `sent_ts` and `sent_channel`: the Slack message the picture was last posted
  after, and its channel. The notifier uploads after the message's text, into
  the same channel and thread.
- `deleted_at`: set when the picture expires.

The housekeeping agent's `before` step, `src.brain.media.expire`, sets
`deleted_at` on rows older than 30 days and deletes their files. The row stays
as the record, and an expired row is never posted again. The table holds no
image bytes.

### `follow_ups`

Things the owner may have left open (migration 17, `alfred-stage-2-3`, created
with `media`). Columns: `id` (`F-` and six digits, the next number), `source`,
`ref`, `asked_by`, `place` (`shared` or `private`), `summary`, `status`,
`first_seen`, `last_raised`, `research` and `dedupe_key`.

`dedupe_key` is unique: `upsert_follow_up` adds a row with status `open`, or, when
the key exists, refreshes that row's `summary` and keeps its id, status and
`first_seen`. `update_follow_up` sets the status and any other field it is
given; the statuses are `open`, `raised`, `done`, `dismissed` and `expired`.
`expire_follow_ups` sets `expired` on `open` and `raised` rows first seen
before a cutoff. All access goes through these helpers in `src/db.py`.

### `action_note_task_links`

Stable association between a task ID and an action-note ID. Completion tools
validate this link rather than treating IDs extracted from free-form task text
as authority.

### `daemon_state` and `task_hashes`

Small operational checkpoints and task idempotency records.

### `garmin_days`

Optional daily Garmin metrics and activity JSON. The Garmin code job owns
writes; agent tools are read-only.

## Separate housing database

Housing search uses `housing.db` beside the main database. It contains:

- normalized `listings`;
- saved `watches`;
- `watch_hits` used to surface only new matches.

## Schema evolution

Fresh databases are created by `_init_schema`. Existing installations are
upgraded by the ordered migrations in `src/db.py`; each successful version is
recorded in `schema_migrations`. Initialization takes a SQLite
`BEGIN IMMEDIATE` lock around the complete migration pass, and each version also
runs inside a savepoint. A failure rolls back both its schema changes and ledger
entry. A binary refuses to open a database containing a newer schema version.
Never reuse or rename a released version. Keep schema changes in this central
migration list, with fresh-schema, legacy-backfill, concurrent-startup,
idempotency, and rollback tests.

Retired tables such as `transcripts`, `daily_note_sections`, `interests`, and
`search_history` are no longer created or read. Existing installations may
still contain them; remove them only through an explicit, backed-up maintenance
migration.

The tables of a removed review app
(`mc_items`, `mc_action_notes`, `mc_holds`, `mc_responses` and `mc_mirror`,
migrations 14 and 15) are still created by those migrations but no longer
written or read, and `briefs` is no longer written; old rows stay in place.
Events with actor `mission_control:<area>` are old review receipts from it.

## Useful queries

```sql
-- Recent failed/partial agent runs
SELECT completed_at, agent, status, error
FROM agent_runs
WHERE status != 'success'
ORDER BY completed_at DESC
LIMIT 25;

-- Active action queue
SELECT id, type, risk, status, origin_area, created_at
FROM pending_actions
WHERE status IN ('pending', 'approved', 'executing')
ORDER BY created_at;

-- Recent important findings
SELECT created_at, agent, category, title, action
FROM findings
WHERE category IN ('URGENT', 'IMPORTANT')
ORDER BY created_at DESC
LIMIT 50;

-- Integration heartbeats
SELECT key, value FROM daemon_state ORDER BY key;
```

Before manual maintenance, stop the daemon and take a SQLite-consistent backup.
Never copy only the main file while live WAL writers are active.
