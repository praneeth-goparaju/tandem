# Integrations

The plugin layer abstracts Slack notification/listening and task management.
Other integrations are direct brain tools or scheduler code jobs.

## Vault connector

The vault connector serves the vault to the Claude app and needs no
integration at all. Its Apple integrations are switched one by one in
`user/vault_connector.yaml`; each is off when its key is missing:

```yaml
integrations:
  reminders: eventkit   # or off
  calendar: caldav      # or off
  mail: imap            # or off
  contacts: carddav     # or off
```

The tools behind these switches live in `src/brain/connector/integrations/apple.py`
and reuse the iCloud and Reminders code described below. The connector's alert
list is `alert_lists.household` in the same file; reminder routing for people and
topics is `reminders.routing` in `user/profile.yaml`. `python -m src.cli doctor`
reports which integrations are on. Setup is in `docs/vault-connector-setup.md`.

## Zero-secret development

```yaml
# config/integrations.yaml
notifier: noop
tasks: noop
```

`make demo` points `USER_DIR` at the tracked PhD-student example and performs a
dry run with noop integrations. It does not modify `user/` or shipped config,
and no message or task leaves the machine.

## Slack

One Slack app posts notifications and receives replies through Socket Mode; no
public HTTP endpoint is needed.

### App setup

1. Create an app from the tracked, generic
   `config/slack_app_manifest.yaml` manifest.
2. Install it to the workspace and invite it to every destination channel.
   Existing installations must reinstall/reauthorize after pulling the
   `files:read` scope (without it, agents receive text but cannot read Slack
   images or voice clips) and the `files:write` scope (without it, charts and
   drawings cannot be uploaded and Slack answers `missing_scope`).
3. Create an app-level token with `connections:write`.
4. Add secrets to `.env`:

   ```bash
   SLACK_BOT_TOKEN="xoxb-..."
   SLACK_APP_TOKEN="xapp-..."
   OWNER_SLACK_USER_ID="U..."
   ```

5. Configure the plugin in the gitignored `user/integrations.yaml` overlay
   (do not put workspace IDs in tracked `config/integrations.yaml`):

   ```yaml
   notifier:
     type: slack
     options:
       vault_name: "Notes"
       default_channel_id: "C0123456789"
       # Private overrides for the placeholder destinations shipped in agent
       # frontmatter. Keys must name registered, Slack-enabled agents.
       channels:
         alfred: {channel_id: "C0234567890"}          # the shared channel
         discovery: {channel_id: "C0234567890"}       # an area on that channel
         alfred_private: {channel_id: "C0234567891"}  # the owner's private channel
       authorized_senders: {}
   ```

Each agent’s display identity and Slack delivery capability are declared in its
frontmatter. Leave `icon_emoji` out so messages show the app's own icon, the
same one Slack shows on uploaded pictures, which cannot carry a custom icon. Shipped channel IDs may be placeholders; keep real workspace IDs
private by replacing them through `options.channels` as shown above:

```yaml
identity:
  display_name: "Alfred"
delivery:
  slack:
    channel_id: "C0123456789"
```

The private override becomes the effective registry destination for both
outbound delivery and inbound authorization. `default_channel_id` is for
unscoped system messages, not normal area routing.
Agents share a channel on purpose: a channel is a place, and the agents
delivered to it are the place's areas. Approvals stay bound to the originating
agent. The agent whose frontmatter sets `chat` answers every message in its
channel (see below).
Vault `[[wikilinks]]` are converted to `obsidian://` links using `vault_name`.

### Places, replies and authorization

Alfred talks in two places. The shared channel is a private channel where the
owner and the co-owner write. A private channel only the owner is in is the
other; direct messages with the bot are off (the app's Messages tab is read-only). A place is the channel of its chat agent, the agent whose frontmatter
sets `chat: shared` or `chat: private`, and every agent delivered to that
channel is one of the place's areas. The place, not the sender, decides what a
chat may use:

- The shared place gives the run only the areas delivered to that channel,
  whoever is asking: no mail or contacts tools, no `search_knowledge` or
  `ops_summary`, vault reads limited to those areas' `vault_paths`, and findings and proposals
  filtered to those areas.
- The private place has everything its areas have, and it is the owner's alone.

Every message goes to Alfred, the chat agent of the channel it arrives in; there
is no routing to area agents. Thread replies are correlated through Slack
metadata and the notifications table, so a reply keeps the area of the message
it answers: that area's notes and approval cards are used. A new message gets an
index of the place's areas instead. A channel that agents deliver to but that has
no chat agent answers “This channel is not set up for Alfred yet.”

The exact `OWNER_SLACK_USER_ID` is the only identity with owner authority;
display names never grant it. A direct message from anyone else is refused with
a pointer to the shared channel. Additional humans are scoped by area:

```yaml
authorized_senders:
  U0123456789:
    name: "Co-owner"
    areas: [alfred, discovery, housing]
```

List every area delivered to the shared channel, the chat agent `alfred`
included: a new message is authorized against the chat agent's own area, a
thread reply against the area of the message it answers, and a chat in the
shared place needs all of them. A list that misses one is refused, and a
non-owner never reaches a run that reads the whole vault whatever the list says.
An empty list grants no areas, and `areas: "*"` grants every area. Every entry
must use the object form and explicitly include `areas`; bare strings and omitted
scopes fail closed. Approval authorization is additionally bound to the action’s
stored origin, audience, channel, payload hash, and version. Every session a
non-owner starts is journaled as `co_owner_session`.

Slack images are downloaded only after sender and physical-channel
authorization. Downloads accept JPEG, PNG, GIF, or WebP from `files.slack.com`,
follow no redirects, ignore ambient proxies, and are limited to four images,
10 MiB each and 20 MiB total. Images and quoted/thread content taint the run:
direct mutation tools are blocked, while queued effects require confirmation.

### Voice notes

A Slack audio or video clip, or an attached audio file, of up to 10 minutes and
25 MiB is downloaded the same way, but only from the owner or an authorized
sender, only when the sender uploaded it, and transcribed on this Mac, one clip
at a time. An attached video is not a clip. The clip's type is decided from its
bytes, `ffmpeg` reads it only as that type and converts it, a clip that plays
longer than 10 minutes is refused, and `whisper-cli` transcribes the rest. The
transcript of a Slack clip becomes the
sender's message, prefixed `[voice note, transcribed] `, and is not outside
content. The transcript of an attached audio file is prefixed
`[attached audio, transcribed] ` and is outside content, like an image. Setup:

1. Install the tools: `brew install ffmpeg whisper-cpp`. The daemon's `PATH`
   must reach them; the shipped launchd template includes `/opt/homebrew/bin`.
2. Download `ggml-small.bin` from the whisper.cpp model repository into a folder
   outside the vault and the repository.
3. Set `BRAIN_WHISPER_MODEL="/absolute/path/to/ggml-small.bin"` in `.env`.
4. Put the file's SHA-256 (`shasum -a 256 <file>`) in `config/media.yaml`, in
   place of `TO-BE-PINNED`. The doctor warns until the hash is pinned and
   whenever the file does not match it, and, while `BRAIN_MEDIA` is on, until
   `BRAIN_WHISPER_MODEL` is set.
5. Restart the daemon.

Without a model path a clip is answered with “I could not read that voice note.
Please type it.” A clip over the limits is answered with “That clip is over 10
minutes or 25 MB; please send a shorter one.” When the message also has typed
text, the text is answered instead, with the line “[a voice note was attached but
could not be read]” under it. `BRAIN_MEDIA=0` turns voice notes off together with
the drawing tools.

### The morning cards and the Home tab

The closing run posts each place's morning with `send_morning_cards`: an
overview message, then one message per thing (an emoji, a bold title and one
line). Its first thread reply holds the detail (a heading, a quote line, short
bullets, a grey source line) and the buttons: Done, and for a follow-up also
Tomorrow and Drop, plus up to two of an approval for a pending proposal of the
card's area and a question to Alfred. Clicks arrive over Socket Mode as
`block_actions` and are handled in `src/orchestrator/card_actions.py`: only on
the card's own thread reply, from the owner in the private channel or from a
sender the shared place allows in the shared one; an approval goes through the
normal approval checks, and a question is answered in the card's thread.

In the shared channel Alfred answers only when addressed: a message that says
"Alfred" or mentions the bot, a voice note, or a reply in a thread Alfred started
or that began by addressing Alfred. Chat between the people there gets no reply.

While Alfred works on a message it puts 👀 on it and takes it off when the answer
is posted (scope `reactions:write`; without it the reply still works).

The Home tab (`src/brain/followups/home.py`) is published when the owner opens
it (`app_home_opened`), after the night shift, after the morning run and after
each card click. Anyone else sees one line. The shipped manifest enables
the Home tab and the `app_home_opened` event and has no `im:` scopes; reinstall
the app after changing them.

## Tasks

The generic task tools use `TaskManager`:

- `tasks_list`
- `tasks_create`

Agents can read tasks and queue idempotent task creation. Apple Reminders can
also be completed by exact EventKit identifier through `complete_reminder`;
the action is confirmation-gated and its saved completion state is read back.
Other task updates stay user-owned in the configured task app.

### Apple Reminders

```yaml
tasks: apple_reminders
```

This backend uses EventKit on macOS. The exact Python executable used by the
daemon needs one-time Reminders permission. Run `brain doctor` after granting
it to verify the round trip.

macOS checks that permission against the process it holds responsible for the
request, not against the Python executable. The launchd daemon is responsible
for itself and holds the grant. A bridge process started from the Claude
desktop app (a routine, or a desktop session using the gateway) is attributed
to the desktop app, which macOS refuses for Reminders without showing a
prompt. `propose_action` therefore checks the Reminders status first, without
prompting. When its own process has no access, it approves a `create_reminder`
or `complete_reminder` action and leaves it for the daemon's executor, then
waits up to 30 seconds and returns the executor's result. If the daemon is not
running, the action stays `approved`, the caller is told it has not run yet,
and it runs when the daemon next starts. `reminders_list` and `reminders_get`
have no such hand-over and still fail in a desktop-app bridge.

The persisted action type `create_todoist_task` is a historical internal name;
the handler routes it to the active task backend and must not be renamed while
old action rows exist.

### Todoist compatibility

```yaml
tasks: todoist
```

Set `TODOIST_API_TOKEN` in `.env`. Prefer Apple Reminders for new installations;
Todoist remains for existing deployments and action records.

## iCloud

Set the Apple ID in `.env`, and keep the app-specific password in either `.env`
or the login Keychain:

```bash
ICLOUD_APPLE_ID="..."
ICLOUD_APP_PASSWORD="..."    # leave empty to use the Keychain instead
ICLOUD_IMAP_USER="..."       # only when mailbox identity differs
ICLOUD_FROM_ALIASES="me@icloud.com,me@example.com"
```

If both `ICLOUD_APPLE_ID` and `ICLOUD_APP_PASSWORD` are set in `.env`, those
values are used. If `ICLOUD_APP_PASSWORD` is empty, the password is read from
the login Keychain item `brain-icloud-app-password` for that Apple ID. Add it
once, in a terminal, and type the password at the prompt so it is never in a
file or in a chat:

```bash
security add-generic-password -s brain-icloud-app-password -a "<apple id>" -w
```

The password is read once per process and kept in memory only. This app
password also allows sending mail through SMTP, which is one reason to keep it
out of `.env`. The brain tools have no SMTP code.

The tools use:

- CalDAV for calendar reads and approved mutations;
- CardDAV for contacts;
- IMAP for mail reads and approved archive operations;
- EventKit for Reminders.

All authenticated CalDAV/CardDAV requests require HTTPS and stay on allowlisted
iCloud hosts across redirects. Mail/calendar/contact fields are untrusted model
input and are wrapped accordingly.

## Gmail mail

The inbox tools can read a configured Gmail mailbox through IMAP:

```bash
GMAIL_ADDRESS="..."
GMAIL_APP_PASSWORD="..."
```

Use an app-specific password and restrict `.env` to `0600`. The provider is
selected by the generic mail tool arguments; it is not a model provider.

## Public discovery collector

External discovery is deliberately split into two trust domains. A
deterministic code collector reads only `user/public_discovery.yaml`, sends its
generic public queries to Tavily Search, and quarantines results as untrusted
intake rows. It cannot derive queries from the vault or profile. The discovery
agent privately compares quarantined candidates with personal context after
collection. Its separately allowlisted, on-demand web tools are bounded as
described below and do not replace this vault-blind scheduled collection path.

Copy the public-safe example and configure a Tavily API key:

```bash
install -m 600 examples/phd_student/public_discovery.yaml user/public_discovery.yaml
TAVILY_API_KEY="..."
```

Review every query in that file as information you are comfortable disclosing
publicly, then set `enabled: true`. Keep names, exact addresses, contacts,
employers, private projects, raw note text, and sensitive health/financial facts
out of the catalog. Coarse public regions and broad topic categories are the
intended inputs. Tavily can still observe the public query, source IP, and query
timing; this is data minimization, not network anonymity.

The collector rotates through the query catalog on its configured cron,
restricts requests and responses, rejects redirects, and deduplicates candidates
by query/result content. `brain doctor` rejects enabled configurations without
a valid schedule, at least one query, or the required credential.

### Agent public-web research

Agents explicitly granted `mcp__brain-tools__web_search` and
`mcp__brain-tools__web_read` use the same `TAVILY_API_KEY`. A search call accepts
one query, or at most two distinct queries for a compound request, and returns
at most five opaque result IDs per query. Results are grouped by query, stored
for 20 minutes, and bound to the authenticated agent and area. `web_read`
accepts only those IDs, never an arbitrary URL.
Queries that explicitly contain `news` or `headlines` use Tavily's news mode;
those news queries also use a one-day freshness window when they contain `today`.

The tools exist only in reply allowlists and also reject non-reply callers.
Scheduled research continues through the vault-blind public-discovery catalog.
Each agent run permits three search tool calls; a two-query batch consumes two search
quota entries. In the shared place the searches must come before any other tool
call, and every query word must appear in the Slack request. In the owner's
private place a search may come later and use any plain words, except
credential-shaped text and numbers of six or more digits. From the first web
call, a default-deny runtime hook allows only page reads, the authenticated
reply, source-framed findings, proposals and the read-only tools in
`READS_AFTER_WEB` (vault reads, calendar, reminders, tasks, findings,
follow-ups, area notes, Garmin); proposals are forced to `risk=confirm`. Once
one of those reads has run after web content, no further search is allowed, so
a page cannot get what was read into a query.

The reader permits credential-free HTTPS on port 443 only, pins the connection
to prevalidated public DNS, refuses cross-domain redirects and ambient proxies,
bounds HTML/plain-text responses, and removes active markup. Query validation
rejects common private-data and credential shapes. Admitted tool attempts
and outcomes are audited by content hash; quotas are transactional. Search is
the first tool call and its words are confined to the authenticated current
Slack message plus fixed public-lookup modifiers. Quoted/thread text and earlier
private reads cannot become search-query egress.

Snippets and pages remain untrusted `external_content`: they may supply cited
evidence, but never instructions, authorization, or new capabilities.

## Intake

The `intake` table quarantines content that comes from outside. The public
discovery collector is the scheduled writer, and its rows enter as untrusted
`web_candidate` items. No scheduled job drains a remote store into it.

Intake content remains `trust='untrusted'`. It reaches durable vault knowledge
only through a confirmed promotion action.

## Garmin

Install the optional dependency and explicitly enable the integration:

```bash
.venv/bin/pip install -e '.[garmin]'
GARMIN_ENABLED=1
GARMIN_TOKENSTORE="$HOME/.garminconnect"
```

Authenticate interactively once using `garminconnect` so the token store
exists. The scheduler registers Garmin jobs only when enabled and configured.
Token refresh is serialized and written atomically with private permissions.
Endpoint groups are fetched independently so a partial Garmin outage does not
discard every metric for the day.

Read-only agent tools are `garmin_daily` and `garmin_trends`. Both cover
sleep-stage breakdown (deep/light/REM/awake seconds) and naps alongside
steps, resting HR, HRV, stress, body battery, and activities — the nap
fields (`nap_seconds`, `nap_start_local`, `nap_end_local`, `nap_feedback`)
come from the same sleep endpoint the sync already calls, no extra Garmin
request.

The sync also stores every available daily health/performance payload from
heart rate, detailed stress and Body Battery, respiration, Pulse Ox, intensity
minutes, training readiness/status, endurance score, fitness age, floors,
hydration and body composition. Frequently useful values are normalized into
columns; lossless endpoint payloads live in `health_details`. `garmin_daily`
returns the compact fields by default and accepts `include_details=true` for
the high-frequency/raw data. Empty Garmin features remain null or empty and
must not be interpreted as zero measurements.

A separate, unexposed capability: `garminconnect.Garmin.get_heart_rates(date)`
returns ~2-minute-resolution heart rate for the whole day, useful for finding
the reading during a specific window (e.g. a gym session). It is not synced
into `garmin_days` or wrapped as a tool — pull it live via
`src.orchestrator.garmin_sync._default_garmin()` when needed.

## People diary and occasions

People notes and occasion configuration live under the private user/vault
surfaces. Scheduled scans create Slack reminders with suggested wording.

## macOS services and backups

The repository ships two sanitized launchd templates:

- `ops/com.example.tandem.daemon.plist.example`
- `ops/com.example.tandem.backup.plist.example`

launchd does not expand shell variables or `~` in plist values. Render every
`__PLACEHOLDER__` to an absolute path in a local copy, create the selected log
directory with mode `0700`, and validate the result with `plutil -lint` before
installing it. Both templates set a private `0077` umask. Application logs
rotate at 10 MiB with five retained files; launchd stdout/stderr paths capture
only output outside the configured Python logger.
The backup template additionally requires an already initialized
`RESTIC_REPOSITORY` and a separately recoverable `RESTIC_PASSWORD_FILE`.
Neither template is installed by `make install`.

After changing code or the built runtime, restart the installed daemon:

```bash
launchctl kickstart -k "gui/$(id -u)/com.example.tandem.daemon"
```

After changing a plist, run `launchctl bootout` for the existing job and
`launchctl bootstrap` with the rendered plist so launchd reloads its settings.

Do not treat a scheduled backup as recovery proof. Complete and record the
operator drill in [RESTORE_DRILL.md](RESTORE_DRILL.md), including an off-machine
repository, password recovery, SQLite integrity check, and daemon smoke test.

### Pausing the agents

Set `BRAIN_AGENTS_PAUSED=1` in `.env` and restart the daemon to run it as a
background service with no agent runs:

- no mode from `modes.yaml` is scheduled, and the morning catch-up is skipped;
- a conversational Slack reply is answered with one fixed line and starts
  nothing;
- the action executor, approval buttons, "yes"/"no" approval replies, keyword
  replies such as "done", and the code jobs (Garmin sync, public discovery, backup
  freshness check) keep running.

`python -m src.cli --run <mode>` still runs a mode by hand. Remove the
variable and restart to bring the scheduled agents back.

### Retiring Slack

`BRAIN_SLACK_OFF=1` turns the Slack notifier into `noop` and starts no Slack
listener, without editing `integrations.yaml`.

## Adding a plugin backend

Only add a plugin when callers need to swap implementations behind an existing
protocol.

1. Implement `Notifier`, `ReplyListener`, or `TaskManager` from
   `src/brain/plugins/base.py`.
2. Add the implementation to the matching built-in registry in
   `src/brain/plugins/loader.py`.
3. Add protocol conformance and loader tests.
4. Document its options and secret handling.

Do not add a parallel plugin abstraction for a one-off direct tool or scheduler
job.

## Troubleshooting

Run `.venv/bin/brain doctor` first. Common checks:

- Slack replies require both `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
- The bot must be invited to private channels.
- A channel that several agents deliver to needs exactly one chat agent.
- A co-owner who gets no answer in the shared channel needs every area of that
  channel, `alfred` included, in their `areas` list.
- Apple Reminders permission applies to the daemon’s Python executable, not
  merely Terminal.app.
- iCloud uses the Apple ID for CalDAV/CardDAV and may require a distinct
  `ICLOUD_IMAP_USER` for mail.
- Garmin jobs do not register unless `GARMIN_ENABLED=1` and the optional
  dependency/token store are available.
