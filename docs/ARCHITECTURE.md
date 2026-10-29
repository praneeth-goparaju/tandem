# Architecture

Tandem is a local Python service that runs Claude Code sessions through
the Claude Agent SDK. Every agent speaks as one assistant, Alfred.
Configuration describes agents and schedules; Python remains authoritative for
policy, persistence, integrations, and world effects.

## Components

```text
config/ + user/ overlays
          |
          v
  loader + AgentSpec -----> scheduler / CLI / Slack reply listener
                                   |
                                   v
                           ClaudeRunner (Python)
                                   |
                       Claude Agent SDK session
                                   |
                                   v
                       Claude Code (bundled CLI)
                         |                 |
                   Read/Glob/Grep     brain-tools server (in-process)
                                           |
                                     Python bridge (one process per call)
                                           |
                 +-------------------------+-----------------------+
                 v                         v                       v
              SQLite                    vault                   plugins
                                                           Slack / tasks
```

`Edit`, `Write` and `Agent` join the left branch for the agents whose
frontmatter grants them (see Direct edits and helpers).

The vault connector (`src/brain/connector/`) is a separate process and the
front door for people who use the Claude app: a Streamable HTTP MCP server on
127.0.0.1, reached through a Cloudflare Tunnel, with its own OAuth sign-in and
two endpoints (`/mcp` full, `/mcp/limited` for scheduled tasks). It does not use
the runner or the agents. Its vault tools are in `tools.py`; the Reminders,
Calendar, mail and contacts tools are in `integrations/apple.py` and are offered
only when their switch under `integrations:` in `user/vault_connector.yaml` is
on. The vault layout comes from `notes:` in `user/profile.yaml`. Its launchd
jobs are the templates in `deploy/launchd/`, written out by
`python -m src.cli render-launchd`, and `python -m src.cli init-vault` copies the
starter vault in `templates/vault/` into a folder. The design is
`docs/vault-connector-design.md`; setup is `docs/vault-connector-setup.md`.

## Configuration and prompts

`src/brain/runtime/loader.py` merges shipped configuration with the private
`user/` overlay:

- `config/agents/*.md` plus `user/agents/*.md`, overridden by agent name;
- `config/modes.yaml` plus `user/modes.yaml`, overridden by mode name;
- `user/profile.yaml` and `user/sources.yaml`;
- `config/integrations.yaml`;
- allowlisted standing vault instructions.

An agent file contains YAML frontmatter and a Jinja prompt body. `AgentSpec`
validates the frontmatter. `template.py` supplies date/time, profile, source
lookup, active mode, expected-agent context, and `caps`, what this run may do
(direct edits, how many helpers, which `vault_paths`). Undefined Jinja variables
are errors.

The preamble includes the rule fragments in `config/agents/_rules/` according
to `caps`, so a prompt never describes an ability the session does not have:
`vault_write.md` for a run that may edit directly, `vault_read_only.md` for one
that may not, and `helpers.md` for one that may start helpers. `caps` follows
the same grants and feature switches the runner applies when it offers the
session its tools.

Each area's prompt carries its area notes between the Jinja comments
`{# area-notes #}` and `{# /area-notes #}`: what the area covers, where its
files are, and its rules. The rest of the prompt is the area's morning
checklist, used only by unattended runs. `area_notes.py` extracts the block,
and a chat reads it through the `area_notes` tool.

`src/orchestrator/registry.py` turns validated specs into the compact mapping
consumed by dispatch. It resolves the effective tool allowlist and strips Slack
push/reply tools from agents without a Slack delivery target.

## Scheduling and dispatch

Each mode has up to four parts:

1. `pre`, sequential;
2. `parallel`, dispatched with `asyncio.gather`;
3. `sequential`, after the parallel group;
4. optional `chain`, launched only after the mode completes.

The scheduler registers cron expressions from the merged mode configuration and
separate code jobs such as public discovery and optional Garmin sync. Manual runs use
the same executor path.

An agent whose frontmatter names `before: src.<module>` runs that module first,
as `python -I -m <module>` in the repository root. It is a plain process, never
a Claude session, with a 120-second timeout and an environment of `PATH` plus
`HOME`, `USER_DIR`, `BRAIN_VERIFY_RUN`, `MEMORY_DB_PATH`, `VAULT_DB_PATH`,
`VAULT_PATH` and `BRAIN_SLACK_OFF` when they are set. A non-zero exit, or a timeout, is logged and the
session still runs. `housekeeping` uses it to expire media. The step starts only
after the checks the session itself makes: no API credential is set and, in a
verification run, `MEMORY_DB_PATH`, `VAULT_PATH` and `USER_DIR` are copies
(each holds a `.brain-verify-copy` marker). A failed check stops the agent, so a
before step never reaches live state from a misconfigured run.

`ClaudeRunner` is the only runner. It checks that no API credential is set,
splits the effective tools, supplies authenticated caller context, starts one
Claude Agent SDK session with nothing loaded from the owner's own Claude setup,
applies the rules in `run_guard.py` before every tool call, applies the
deadline, and persists one `agent_runs` row. Only one session runs at a time
across the daemon, so a mode's `parallel` group runs one agent after another. A
normal empty completion is a successful quiet scheduled run; an inbound reply
still requires text or a verified `reply_to_message` delivery receipt.

See [PROVIDERS.md](PROVIDERS.md) for the runtime contract.

## The morning cycle

A morning mode runs its agents one session at a time, in this order:

1. **Housekeeping** (`pre`): routes Quick Captures, closes finished Actions
   notes, routes dismissals, and checks the vault for contradictions and stale
   items. Its routing changes are `edit_vault_file` proposals, and it never
   posts.
2. **Area runs** (`parallel`): one short session per area with that area's
   notes and tools. Each stores findings and proposals. The starter kit ships
   `discovery`; a deployment adds its own areas in `user/agents/`.
3. **Narrow service runs**, such as intake triage, when the mode lists them.
4. **Follow-up run** (`follow_up`, in `sequential` just before the closing run):
   collects what is new in Slack and in Claude Code transcripts and raises up to
   five questions nobody answered, promises not kept and decisions left open
   (see Follow-ups). It never posts.
5. **Closing run** (`alfred_private`, the private chat agent, last in
   `sequential`): reads the morning's findings, pending proposals, changed
   files and raised follow-ups, checks across the areas, and posts one message
   to each place with `send_morning_cards`. It has no web tools. The cross-area findings and
   proposals it stores are the owner's alone, which is why it runs as the
   private chat agent: they stay in the private place.

A mode has a closing run when its last `sequential` agent is the private chat
agent (`chat: private`). Every other run in that cycle loses `push_notify`,
`mail_reply_link` (which can post to Slack itself) and the `push_notification`
action type, so it cannot post by any route: its findings and proposals are what
the closing run reports; `closing_solo` runs the closing run alone. A mode
without a closing run (`scout`, the other manual solo modes) keeps each agent's
own posting, under Alfred's name, to the area's place.

`send_morning_cards` takes a `place` (`shared` or `private`), an `overview`
and a list of `cards`: the closing run's prompt asks for at most 8 per place and
the tool refuses more than 12. The overview and each card are their own
top-level message, and each card's detail and buttons (Done, Tomorrow, Drop,
approvals, questions to Alfred) are in its thread. Slack threads are flat, so a
later reply in a card's thread comes back to Alfred in that place, which reads
that area's notes before answering. A card's approvals show only for proposals
that are pending and were made by the card's own area. The tool refuses, and
sends nothing, when a card's area belongs to the other place, a shared card
carries a private follow-up, or a card's text names a follow-up id. An agent without Slack delivery, such as
housekeeping, belongs to the private place. Only the private chat agent may be
granted the tool, and a chat run of that agent cannot call it. In a
verification run (`BRAIN_VERIFY_RUN=1`) it writes `morning-<place>.md` next to
the copied database instead of sending.

Before it writes the cards, the closing run calls `night_report` (in
`tools/follow_ups.py`): what the last night shift did and every raised
follow-up with its research and proposals, which become the follow-up cards
and one overview line on the night's work. It also calls `handled_cards`: the cards
the owner answered with Done or Drop in the last 14 days, and put off with
Tomorrow that same day. It does not post those subjects again unless something
new happened, because a button changes only its own card and the findings
behind it stay.

## Follow-ups

The `follow_ups` table is the one to-do list. The night shift adds what
conversations left open; the closing run and the chats add anything else open
with `follow_ups_open`, which returns the existing id for the same summary and
says when that item was already closed. Every morning card about something
open carries its item id (`send_morning_cards` refuses one without, except the
`discovery` and `alfred_private` cards), so Done, Tomorrow and Drop act on the
item. `close_follow_up` in `src/brain/followups/close.py` is the only way an
item closes: a card button, a `done F-…` reply and `follow_ups_update` all call
it, and it proposes `complete_reminder` for every Apple Reminder the item's
proposals created (`action_ids`), which runs without a tap. The owner's own
Claude sessions read and change the list through the gateway's `trusted`
profile (`follow_ups_list`, `follow_ups_open`, `follow_ups_update`).

**Smart reminders.** In a chat, `remind_me(summary, when, area, context)` puts
the thing on the list (or uses the named item), saves the time, the Slack
thread and what was said on it (migration 20 adds `remind_at`, `reminded_at`,
`thread_channel`, `thread_ts`, `reminder_context`), and proposes a
`create_reminder` for Apple Reminders whose id joins the item's `action_ids`.
Every five minutes the daemon's `smart_reminders` job
(`src/orchestrator/reminders.py`) claims each due item once and hands
`reply_routing.on_reply` a message in that thread from the person who asked
for it (`reminder_requested_by`, migration 21), so the run has that person's
rights, not the owner's by default. The message text is fixed; the stored
summary and context come back only as quoted text, which the run treats as
outside content, so they can never act as an instruction or an approval. Closing the item ticks the Apple Reminder too.

`src/brain/followups/` finds what was asked, promised or left half done and
never closed. It is plain Python, never a Claude session: `transcripts.py`
reads Claude Code transcripts under `~/.claude/projects`, `slack_history.py`
reads the two places' Slack channels (and the retired per-area channels until
`old_channels_until`), `scrub.py` removes secrets, `digest.py` writes the digest
and keeps the cursor, and `collect.py` runs them.

**The collector step.** The `follow_up` agent names `before:
src.brain.followups.collect`, so the executor runs `python -I -m
src.brain.followups.collect` as a plain process right before that agent's
session: from the repository root, with `PATH` and the few variables it needs
and nothing else of the daemon's environment, and 120 seconds to finish (the
collector then loads the repository `.env` itself, as every process that imports
`src.db` does, and reads the bot token and `OWNER_SLACK_USER_ID` from it). A
non-zero exit or a timeout is logged with the last 500 characters of its stderr,
and the session still runs. No before step runs while the usage limit holds,
because the session is skipped then. The collector reads what no successful
follow-up session has read yet, up to the moment it started (14 days back on the
first run). From Slack it keeps people's messages, a file's text included, named
`owner`, `co-owner` (an authorized sender) or `user:<id>`, and the plain posts
of bots, named `alfred` for this app's own bot (from `auth.test`) or `bot:<id>`;
it drops files, system messages and Alfred's own morning messages, which carry
the `morning_message` metadata `send_morning_cards` posts them with. A reply's
heading ends with `thread:` and the first 100 characters of the message that
opened its thread. From transcripts it keeps what the owner typed and Claude's
text; it drops tool calls and results, helper and SDK sessions, sessions run
under `~/.claude`, every project folder listed under `exclude_projects` or named
after one and `-` (its worktrees and subfolders), every line whose working
directory has a component listed under `exclude_paths`, and any symlinked folder
or file. It scrubs secrets, cuts a person's message at 2,000 characters and
Claude's or Alfred's at 800, closes a code fence an entry leaves open, and
writes `$USER_DIR/followups/digest-<date>.md`, newest first and at most 200 KB,
and beside it `index-<date>.json`, which gives every entry's ref its source and
place (folder `0700`, files `0600`, the last 7 of each kept), then `state.json`,
which holds two cursors, one per Slack channel and one for the transcripts:
`committed`, where every collection starts, and `pending`, where this one
stopped. The executor commits the pending cursor only when the collector exited
0 and the `follow_up` session ended in success, so a window whose session
failed, timed out or never started is collected again into the next digest, and
an item found twice is still one item (`follow_ups_add` returns the known id for
a ref it has). A channel whose history fails keeps its start, and a run that
does not read Slack (no token, or `BRAIN_SLACK_OFF`, which the executor passes
on) leaves the Slack cursor where it was. Slack is read message by message,
oldest first, each with its thread, on a budget: at most 40 thread reads a run,
a rate-limited call retried once, and nothing after 90 seconds from the
collector's start. A rate limit that outlasts the retry, or the end of the
budget, stops Slack for the run, and each channel it did not finish keeps the
window it was reading and the last message it finished in its pending cursor, so
the next run goes on from there instead of starting over. A thread that fails is
logged and skipped. The collector expires `open` and `raised` items first seen
more than 14 days ago and journals `followups_collected` with the counts,
`slack_unfinished` among them. `config/followups.yaml` holds the defaults
(`exclude_projects`, `exclude_paths`, `old_channels`, `old_channels_until`), and
`$USER_DIR/followups.yaml` replaces any key it sets.

**The run.** `follow_up` is a `system` agent, so it belongs to the private
place. It reads the digest with `follow_ups_digest`, which fences it as outside
content (the run can make no direct edit afterwards) and refuses a digest not
dated today. The digest is paged, because one tool result cannot carry 200 KB:
`follow_ups_digest(part=n)` returns at most 40,000 characters, cut between
entries, under a first line `digest <date>, part n of m`, and the prompt has the
run read every part before it decides. The run keeps the `follow_ups` table
through `follow_ups_list`, `follow_ups_add` and `follow_ups_update`. It has no
direct edits and no web tools. It researches at most 3 items and raises at most
5 a morning (the update tool refuses a sixth), stores a finding for each raised
item, and never posts. Each item it raises is journaled as `follow_up_raised`;
the five-a-day limit and the raise are one statement. The closing run calls
`follow_ups_list(status="raised", since=<today>)` and puts the items, each line
starting with its id and ending with how to close it (`reply done F-000012 or
dismiss F-000012 here`), in the morning message of their place under the place's
own area name: `alfred` in the shared message, `alfred_private` in the private
one. Items from the shared channel have place `shared`, everything else
`private`, and the shared place sees only shared items. The run does not choose
that place: `follow_ups_add` takes the `ref` of a digest entry and reads the
entry's source and place from today's index, refusing a ref the index does not
hold, and `send_morning_cards` refuses a shared card that names a private
item's id. A private chat gets the four tools through the union of the private
place's tools; the tools refuse what a chat may not do: a chat cannot add an
item and can only close one, or, in the owner's private channel, reopen a closed
one with status `open`. A closed item (`done`, `dismissed` or `expired`) changes
only by being reopened, and every change is made only while the item still has
the status the tool read, so a run working from an older listing cannot raise
again what the owner just closed.

**The fast path.** In `on_reply`, right after the approval parser and before the
finding keyword fast path and the pause check, two replies close an item (`done`
or `dismissed`) and answer `F-000012 closed`, with no session, off the event
loop, journaled as `follow_up_closed` with who closed it. A reply of exactly
`done F-000012` or `dismiss F-000012` (the keyword in any case, one id, spaces
around it ignored) closes the item it names, wherever in the thread it is
written; this is the form the morning message offers, because Slack threads are
flat and the thread's parent is the summary. A reply of exactly `done` or
`dismiss` in a thread whose parent message starts with an `F-` id closes that
item instead; a parent that lists several ids closes the first, and the answer
names the others. In both forms the parent has to be a bot's message (the
listener reports it as `quoted_from_bot`, true when the parent has a `bot_id` or
the `bot_message` subtype), and the item's place has to fit the reply's place,
taken from the channel's chat agent as dispatch takes it. A reply in the shared
channel closes shared items only, a reply in the owner's private channel closes
any, and a sender who may not use the place closes nothing. Every refusal, and
an id the ledger does not know, leaves the message to the normal path and is
logged at debug. A bare `done` under the summary is such a message: it goes to
Alfred.

`BRAIN_FOLLOWUPS` switches the feature off. With it off the executor skips the
`follow_up` run, collector included, and the session is not offered the four
tools, which the hook refuses anyway. The doctor warns when it is on and
`$USER_DIR/followups.yaml` is missing or excludes nothing, since the collector
then reads every project folder, and when no scheduled mode runs `follow_up`.

## Brain tools

Every brain tool is defined once with `@brain_tool`:

```python
@brain_tool(name="example", description="...", input_schema={...})
async def example(args):
    ...
```

A bootstrap module imports the tool categories so decorators populate
`tool_registry`. The Python bridge (`src/brain/mcp/pi_bridge.py`, named for
the earlier runtime and kept because the `brain-tools-mcp` gateway in
`src/gateway/` calls it)
supports two operations:

- `list`: return serializable tool schemas;
- `call`: validate tool name, per-run allowlist, and caller context, then invoke
  the handler and normalize its content blocks.

The runner's `brain-tools` server offers only the selected names, and each call
runs the bridge in its own process. Thus a tool must pass configuration-time
selection, the runner's tool-call rules, and bridge-time authorization.

## State model

SQLite is the durable operational store. Major surfaces are:

- `findings`: the shared observation ledger; `from_outside` marks a finding
  stored by a run that had received outside content;
- `pending_actions` plus bounded idempotency receipts: proposed, approved,
  executing, completed, partial, ambiguous, and failed work;
- `agent_runs`: one terminal telemetry row per invocation;
- `notifications`: outbound Slack audit/correlation state;
- `intake`: quarantined external captures, and the public-discovery
  collector's web candidates, which only the discovery run reads
  (`public_discovery_candidates`; `intake_pending` leaves them out);
- `events`: append-only audit journal, including `direct_edit`, `edit_refused`,
  `tool_refused`, `helper_start`, `co_owner_session`, `media_rendered` and `media_sent`;
- `vault_edits`: one row per direct `Edit` or `Write`, with the file's SHA-256
  before and after;
- `media`: one row per picture `draw_chart` or `draw_image` rendered, with the
  place it may be posted in, where it was posted and when it expired;
- `follow_ups`: things left open (the questions, promises and open decisions
  the follow-up run found, and items a chat opened), one row for each
  `dedupe_key`, with their status;
- `garmin_days`: optional health metrics;
- `daemon_state` and `task_hashes`: operational checkpoints/deduplication.

Database access goes through `src/db.py`. Ordered migrations are recorded in
`schema_migrations`; the full migration pass is serialized with a SQLite write
lock, each upgrade runs under a savepoint, and binaries reject databases from a
newer schema version. Coverage includes legacy backfill, concurrent startup,
idempotence, rollback, and version reuse.

## One ledger, three doors

Agents do not freely mutate durable knowledge:

1. Observations enter the `findings` ledger with `store_finding`.
2. Tasks and external effects enter `pending_actions`, where central policy
   chooses automatic execution or human confirmation.
3. New durable notes and canonical-file edits use confirmed promotion/edit
   actions with provenance and atomic writes.

A fourth route is narrower than these three: a direct edit inside an area's own
`vault_paths`, made with `Edit` or `Write` and logged in `vault_edits` (see
Direct edits and helpers). It needs no approval card, which is why its limits
are fixed in code and not left to the prompt.

The action record stores immutable origin agent/area, intended approval
audience, payload hash, and version. Slack approval handlers load the record and
authorize the human, area, channel, state, payload, and version in one guarded
transition. Agents cannot call an approval tool.

Handlers return outcomes to the executor. Explicitly retryable, settled
failures are retried under the queue policy; permanent failures are terminal.
An executor deadline is terminal `ambiguous`, because cancellation cannot stop
a synchronous worker already launched with `asyncio.to_thread`. Partial
delivery is terminal `partial`, rather than being hidden inside an executed
result string.

Action deduplication is enforced in SQLite rather than by a process-local
check. Active rows reserve a fingerprint and execution-terminal rows keep
a bounded seven-day receipt. This closes the lost-response replay window for
automatic actions. An explicit immutable `occurrence_key` distinguishes
legitimate repeated business occurrences with identical payloads.

Scoped Slack approval requires a delivery receipt for the exact payload review
before either a button or text command can approve it. When untrusted data
confirmation-gates a scheduled proposal, the proposal layer sends a
deterministic review-only card because a second model-authored notification
would be confirmation-gated too; the card body is fixed and its effect is
rendered from the immutable queue row. `mail_archive_batch` archives up to 50
mails of one account with one approval: the card shows the count and the
senders, and each mail must still match the header snapshot taken when it was
proposed. A run may propose at most three single `mail_archive` actions; more
go in a batch. Approval cards require an explicit area
channel and retry one typed transient delivery once with the same idempotency key.
Proactive presentations sent through `push_notify` remain automatic after read
taint because a confirmation carrier would disclose the same text in the same
channel. They may display separate, immutable same-area approval cards, but do
not approve those actions. This narrow path requires an explicit area
destination and has no occasion marker; tainted raw proposals, tainted replies,
web-derived calls, stateful notifications, and every consequential effect
remain confirmation-gated.

Path-based outbound actions must approve content, not just filenames.
`send_draft` therefore snapshots the confined draft's digest, size, routing
metadata, title, and preview into the immutable action payload. The approval
card renders that snapshot and the handler revalidates the bytes before any
delivery.

Vault-edit proposals similarly freeze the expected old digest and exact final
bytes. Approval is disabled unless the complete unified diff fits in the card;
execution uses confined atomic compare-and-swap writes, and a multi-file edit
stages every target before committing any of them.

## Direct edits and helpers

A direct edit is Claude Code's own `Edit` or `Write` on a vault file. An
agent's frontmatter grants it, and the `PreToolUse` hook in `run_guard.py`
allows a call only when all of these hold:

- The path, with every symlink resolved, is inside the run's `vault_paths`: the
  agent's own for an area run, the union of the place's areas in a chat.
  A folder entry ends in `/` and matches whole path segments, so `Health/`
  does not admit `Health-old/`. A path starting with `~` or `@` is refused,
  since Claude Code and the rule could read it differently.
- The path is not hidden and not off limits: the daily notes, `CLAUDE.md`,
  `VAULT_CONVENTIONS.md`, `Instructions/`, `Brain Agents/` (lessons and voice
  profile), and every file the merged `vault_instructions` configuration loads
  into a prompt. The doctor fails a `vault_paths` entry that is, lies inside, or
  contains one of them.
- `Write` only creates a file: its folder must exist and the content is at most
  256 KB. `Edit` only changes an existing file and may not leave it more than a
  quarter smaller; a larger removal goes through an `edit_vault_file` proposal.
- In an unattended run, no outside content has entered the run. Outside
  content is the output of the mail, calendar, contacts, web, intake and
  listing tools, an image or an audio file attached to a message, and a finding
  flagged `from_outside`. The refusal points to `propose_action` with
  `edit_vault_file`, so the change becomes an approval card. A chat run keeps
  its direct edits after outside content, because the owner is present and the
  reply must name every changed file.

The web rule still applies to every run: after a web search or read, only a
short list of tools stays available, and `Edit` and `Write` are not on it.
Nothing deletes a file. Every completed edit is recorded in `vault_edits` with
the file's SHA-256 before and after (`sha_before` is null for a file the call
created) and journaled as `direct_edit`; a refused one is journaled as
`edit_refused` with the reason. Before an `Edit` or `Write` runs, the file's
previous version is copied to `$USER_DIR/vault-edit-backups/<date>/<vault
path>.<time>` (owner-only), and the edit is refused when that copy cannot be
made; housekeeping's `before` step deletes day folders older than 30 days.

A run granted `Agent` may start helpers, up to its `max_helpers` (3 at most). A
chat may start 2 when one of its place's areas grants `Agent` and
`BRAIN_HELPERS` is on. There is one helper definition, passed through the SDK
`agents` option: `claude-sonnet-5-5`, effort `medium`, at most 15 turns, with
`Read`, `Glob`, `Grep` and the parent's read-only brain tools, and no `Agent`,
`Edit`, `Write`, `propose_action`, posting or web tool: the web rule allows three
searches per run, so web lookups stay with the parent. The hook counts the starts,
so text inside a document cannot raise the limit, and journals each one as
`helper_start`. The SDK fires the session's hooks for a helper's own tool calls,
so confinement, the web rule and the outside-content state apply to helpers and
are shared with the parent: a helper that reads mail ends the parent's direct
edits in an unattended run.

`BRAIN_DIRECT_EDITS` and `BRAIN_HELPERS` switch the two features off. Each run
reads them from the daemon's environment when it starts, so editing `.env`
needs a daemon restart. With one off, the session is not offered the tools, the
prompt leaves out the matching rules, and the hook refuses them anyway.
`BRAIN_MEDIA` and `BRAIN_FOLLOWUPS` work the same way for their tools
(`FEATURE_TOOLS` in `run_guard.py`); `BRAIN_MEDIA` also stops the Slack listener
from handing voice clips to the router. The
Claude session's environment also has the daemon's secrets (names ending in
`_TOKEN`, `_PASSWORD`, `_KEY`, `_SECRET`, `_CREDENTIALS` or `_CREDENTIALS_PATH`,
except the session's own `CLAUDE...` sign-in variables) set to empty.

## Slack, places and chat

The Slack plugin provides outbound notification and Socket Mode listener
surfaces. Per-agent identity and Slack delivery capability live in agent
frontmatter. The private integration overlay may replace placeholder channel
IDs for registered agents; the runtime registry applies that same effective
mapping to outbound delivery and inbound authorization. The integration config
also holds tokens, the system fallback channel and the human authorization
map. Queue-backed push
reports require the explicit area mapping and never fall back to the system
channel.

Alfred talks in two places, and a place is a Slack channel: the shared channel,
where the owner and the co-owner write, and a private channel only the owner is
in (direct messages with the bot are off). Each agent is delivered to one channel, so each area has exactly one place.
On a channel that several agents deliver to, the one whose frontmatter sets
`chat` (`shared` or `private`) is the place's chat agent. Every agent delivered
to the shared channel, the chat agent included, is an area of the shared place.
The private place holds every registered agent: the owner's private channel uses
everything. An agent without Slack delivery, a system agent such as
housekeeping, belongs to the private place, so its approval cards and its
morning item go to the private channel, and a reply there may approve its
proposals. The doctor fails a channel that several agents share and that has no
chat agent, and a place with two.

Inbound replies resolve context from Slack metadata/thread correlation, then
from the channel. Approval-shaped replies are handled deterministically before
model dispatch. Every other message goes to the chat agent of the channel it
arrived in, through the same Claude runner; there is no routing to area agents
and no generalist. A thread whose stored area is no longer a registered agent
goes to the channel's chat agent too. If the run returns text without calling
`reply_to_message`, the router posts that text as a fallback; non-success runtime
results produce a visible failure rather than a false success audit.

`chat.py` builds the run from the place, reading the registry when it is called:

- Tools are the union of the place's areas' `tools` and `reply_tools`, plus
  `area_notes`, minus `push_notify`, `send_morning_cards` and `handled_cards`, the closing
  run's own. Allowed action types and
  `vault_paths` are the unions of the areas' own. For the private place the
  unions run over every registered agent.
- The model is `claude-sonnet-5-5` at effort `medium`, 20 turns and up to 2
  helpers. A property-listing link in a housing thread raises that one reply to
  `claude-opus-5-5`, effort `max`, 60 turns (the housing area reads two German
  portals, so it is useful in Germany only).
- In the shared place, whoever is asking, the run has no `mail_*` or `contacts_*`
  tool, no `search_knowledge` (its ledger has no area column) and no
  `ops_summary` (it reports every area's runs). Nor does it get the action
  types whose proposal or approval card carries mail, contacts or intake:
  `mail_archive`, `mail_archive_batch`, `send_draft`, `create_contact`, `update_contact` and
  `promote_intake`. `propose_action` refuses an `edit_vault_file`,
  `edit_vault_files` or `insert_vault_line` path outside the shared areas'
  `vault_paths` before it reads any file, so an approval card never shows a
  private note. `Read`, `Glob`
  and `Grep` reach only the shared areas' `vault_paths`. The hook resolves a
  path as Claude Code does: a relative path, and a `Glob` or `Grep` without a
  `path`, count from the session's working directory, the vault root, so they
  too must land inside those folders, and a path starting with `~` or `@` is
  refused. The tool server, not the prompt, limits `get_recent_findings`,
  `list_pending_actions` and `list_vault_edits` to the shared areas. The
  private place has none of these limits, and a private chat lists every
  area's pending proposals, as the closing run does.
- The shared place's reply prompt also says what to answer when a question
  reaches outside the place: that it is in a private area, without naming the
  folder, file or area. The private place's prompt does not carry the sentence.

A thread reply keeps the area recorded for the message it answers (the
`notifications` lookup): that area's notes go into the system prompt, and
approval cards use that area. The finding a thread refers to is shown to the run
only when an area of the place stored it. A new message gets a one-line index of
the place's areas, and Alfred reads an area's notes with `area_notes`, which
refuses an area outside the place. Its output is the owner's own notes, so it is
the only tool output the prompt treats as instructions; the other tool output
that comes from outside stays fenced as `<external_content>`.

Authorization comes before any model dispatch. The owner is the exact
`OWNER_SLACK_USER_ID`. A direct message from anyone else is refused with a
pointer to the shared channel, before any context is resolved or attachment
downloaded. Any other sender needs an `authorized_senders` entry, and the shared
place needs every one of its areas, the chat agent included, in that entry's
`areas` list (`"*"` has them all). A non-owner never reaches a run that reads
the whole vault. Every session a non-owner starts is journaled as
`co_owner_session`.

Inbound human messages are trusted as commands only after sender authorization.
Images and voice clips are fetched lazily after that check. The transcript of a
clip recorded in Slack is the sender's own message, not outside content; the
transcript of an audio file the sender attached is outside content, as an image
is. Images, quoted messages, email, calendar, contacts, listings, intake, and
vault content remain untrusted data; runtime taint forces consequential
proposed effects through human confirmation. The direct writers
(`create_draft`, `intake_dismiss`, the people-diary writes and
`watch_search`) are blocked after untrusted content in the shared place and
after web content everywhere. In the owner's private place, where the runner
sets no area limit, a chat keeps all of them after reading mail, the vault or
the calendar, so "draft a reply to this mail" works in one run; an unattended
run keeps only `create_draft`, and a message that arrives with outside content
(an image, an attached audio file, quoted text) keeps none of them. The proactive
fixed-route report exception above changes presentation only, not the trust
assigned to its source data.

## Media

`src/brain/media/` holds pictures out and voice in. A run never sees a file
path: it holds an opaque media id, `m-` and 12 lowercase hex characters.
`BRAIN_MEDIA=0` turns the whole feature off.

**Pictures out.** Two brain tools draw a picture and return its id:

- `draw_chart(spec)` takes numbers and labels only: a title, `line` or `bar`, up
  to 366 labels and up to 6 series with one number per label. `charts.py`
  validates the spec before it draws anything and draws labels as plain text.
  The figure is 1080 by 1350 pixels, portrait so it reads on a phone, on matplotlib's `Agg` backend, drawn with
  matplotlib's own defaults, never the owner's matplotlibrc; matplotlib keeps
  its settings and font cache in a private folder (`MPLCONFIGDIR`, by default
  `brain-matplotlib-<uid>` in the temporary folder, mode `0700`). A missing
  value, a string where a number belongs, `NaN` or infinity is refused with the
  reason.
- `draw_image(svg)` takes SVG text of at most 512 KB. `svg.py` parses it with
  `defusedxml` and refuses, rather than cleans, a document with a `script` or
  `foreignObject`, a DOCTYPE or an entity, an event handler, an `href` that is
  not a `#id` or a `data:` URL holding a PNG, JPEG, GIF or WebP picture (so no
  SVG holds another; the payload's bytes decide, as they do for the renderer),
  an `href` with `url(` in it, or `url(` or `@import` in a style. It renders in a
  child process (`svg_render.py`: an isolated interpreter, `PATH` and `HOME`
  only, a 10-second timeout and a 1 GiB memory limit) whose fetcher serves
  `data:` URLs only, so a reference the check missed fails the render and is
  never fetched. The picture is at most 4096 by 4096 pixels.

`store.py` saves the PNG as `$USER_DIR/media/<id>.png` (folder `0700`, file
`0600`), writes the `media` row and journals `media_rendered`. An id works only
in the place it was made for.

`reply_to_message` and `push_notify` take `media_ids`, at most four, and each
`send_morning_cards` card takes `media_ids` too. After the text is posted, the Slack notifier uploads
each file with `files_upload_v2` into the same channel and thread. It leaves out
an id that does not exist, has expired, has lost its file or was made for a place
other than the destination channel's, and the tool's answer names it under
`refused media`. The channel's place is the place of the chat agent delivered to
it, read through the integrations' channel map; a channel no chat agent has gets
no pictures, and the caller's own place must match it too. Uploading needs the
`files:write` scope; without it Slack answers `missing_scope`, and the answer
names the id under `failed media` with that code. An approved `push_notification`
whose upload failed is recorded as `partial`, not `executed`. In a verification
run nothing is uploaded: the morning file shows `[media <id> <filename>]`.
Housekeeping's `before` step, `src.brain.media.expire`, deletes the files after
30 days and keeps the rows.

**Voice in.** The Slack listener picks the clips of a message that the sender
uploaded (the file's `user` is the sender): Slack clips (a `slack_audio` or
`slack_video` subtype) and attached audio files (an `audio/` type) from
`files.slack.com` that declare at most 25 MiB and, when they declare a length, at
most 10 minutes. An attached video that is not a Slack clip is not a clip, so a
message that carries one is answered as text. It gives `on_reply` a `_load_voice`
loader only for the owner or an authorized sender, and only while `BRAIN_MEDIA`
is on, and marks the message `voice_attached` when the clip is an attached audio
file. A clip over the limits gets a loader that refuses it, so the sender is
told. `on_reply` runs the loader after the sender, area, approval, keyword and
pause checks and before the images:

1. The first clip is downloaded as an image is: bearer authentication, no
   redirects, the size enforced on the bytes.
2. Its type is decided from its first bytes (WebM, MP4, Ogg, WAV, MP3 or FLAC),
   never from the type Slack declares. An MP4 counts only with the major brand
   `M4A `, `mp42`, `isom`, `3gp4`, `3gp5`, `3gp6` or `qt  `, so HEIC and AVIF
   stills are not clips.
3. In a worker thread, in a fresh folder of mode `0700` that is deleted whatever
   happens, `ffmpeg` reads it only as that type (`-f` with the matching demuxer,
   and `file` as the only protocol), never as whatever its own probe would pick,
   and converts at most 601 seconds of it (`-t 601`) and at most 20 MB of output
   (`-fs`) to 16 kHz mono WAV (60-second timeout). A WAV whose header says it
   plays longer than 600 seconds, which is any clip longer than 10 minutes, is
   refused as over the limits before
   `whisper-cli` transcribes it with the model `BRAIN_WHISPER_MODEL` names
   (120-second timeout, language detected). Both run with an argument list and
   an environment of `PATH` and `HOME` only. One clip at a time is downloaded
   and transcribed; a clip sent meanwhile waits its turn.

The transcript of a Slack clip becomes the sender's message, prefixed
`[voice note, transcribed] `, with a typed caption kept above it. It is handled
like typed text except that the keyword fast path (`done`, `skip`) reads typed
text only. The transcript of an attached audio file is prefixed
`[attached audio, transcribed] ` instead, and the message brings outside content
into the run, as an image does: direct mutators are blocked, proposals need
confirmation and the run's findings carry `from_outside`. When a clip is not
audio, cannot be fetched or transcribed, holds no speech, arrives with no model
configured, or is over the limits, typed text still runs, with the line
`[a voice note was attached but could not be read]` under it. With no typed text,
a clip over the limits is answered with "That clip is over 10 minutes or 25 MB;
please send a shorter one.", any other with "I could not read that voice note.
Please type it.", and nothing runs. The model is `ggml-small.bin`, named in
`config/media.yaml` with its SHA-256. The doctor warns, with `BRAIN_MEDIA` on,
when no model path is set (except in CI, `BRAIN_DOCTOR_SKIP_RUNTIME=1`) and, with
the model path set, when the file is missing, does not match the hash, or the
hash is still `TO-BE-PINNED`, and when `$USER_DIR/media` is not mode `0700`.

## Integrations

The plugin layer currently abstracts:

- `Notifier` / `ReplyListener`: Slack or noop;
- `TaskManager`: Apple Reminders, Todoist compatibility, or noop.

Garmin sync is a code job, not an agent runtime. It is optional; its
dependency extra and token store must be explicitly enabled.

Public discovery is also a code job, separated from the private analyst. Its
only outbound inputs come from `user/public_discovery.yaml`; it does not read
the profile or vault. Results enter `intake` as untrusted `web_candidate` rows.
The scheduled collector remains vault-blind, and the discovery agent receives
those candidates through a source-scoped read tool before doing private
relevance analysis.

## Filesystem boundaries

- `user/`, `.env`, databases, and credentials are private and gitignored.
- Database/state directories are private (`0700`) and sensitive files are
  restricted (`0600`).
- General model filesystem access is read-only, except the gated direct edits
  inside an agent's own `vault_paths`.
- The follow-up collector reads `~/.claude/projects`, following no symlink, and
  writes only under `$USER_DIR/followups/`.
- Other vault mutations use confined, atomic action handlers.

## Extending the system

- New agent: add one Markdown file and reference it from a mode. An area also
  needs its Slack channel (its place), its `vault_paths` if it may edit
  directly, and its notes between the area-notes markers.
- New tool: add one decorated handler and its bootstrap import, then grant it
  explicitly to the relevant agents.
- New action: add the enum member, schema/risk policy, handler, exact approval
  preview, retry classification, and state-machine tests.
- New notifier/task backend: implement the relevant protocol and register it in
  the plugin loader.
- New persistent state: add a versioned migration and upgrade tests.
