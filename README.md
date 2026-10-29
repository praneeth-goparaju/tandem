# tandem

A personal assistant for your own Obsidian vault, run on a Mac you own and used
from the Claude app. The front door is the vault connector: a small server on
your Mac that lets your Claude chats, on the phone or the web, search, read and
add to the vault, under fixed rules and with an Approve button for anything
larger. On top of that you can run an agent daemon that works through scheduled
runs, connect Slack, and turn on Apple Reminders, Calendar, mail and contacts.

Everything personal (your name, family, vault layout, lists, domain) lives in a
private `user/` folder that is never committed. The tracked tree holds the code,
the rules, templates and examples.

## Requirements

- macOS on a Mac that stays on (an always-on desktop Mac is ideal)
- Python 3.12 and Homebrew
- A Claude subscription (the connector is added to claude.ai; the agent daemon
  signs in with Claude Code)
- An Obsidian vault, or an empty folder for a new one
- A domain of your own on Cloudflare, for the tunnel that carries claude.ai's
  traffic to your Mac

## What it does

- **Vault connector.** Serves your vault to claude.ai over MCP through a
  Cloudflare Tunnel, with its own sign-in (password plus six-digit code). Two
  endpoints: "Vault" for your own chats and "Vault, limited" for scheduled
  tasks, which has no tool that edits, undoes, updates, completes or archives.
  No tool deletes or moves a note, sends anything or runs a command. Small edits
  run at once; larger ones, and every edit after the chat read mail, wait for
  your Approve tap. Every call is in an audit log and every edit can be undone.
- **Starter vault.** `init-vault` creates a vault from the shipped templates: a
  `CLAUDE.md` rule file, writing-style and capture rules, a daily note template,
  and the open-item format the connector writes.
- **Optional agent daemon.** Runs Markdown-defined agents on YAML-defined
  schedules, each as a Claude Code session through the Claude Agent SDK. Agents
  get read-only vault tools plus the brain tools their file grants; world
  effects go through an action queue (see "Safety boundaries"). Everything speaks
  as one assistant, Alfred. Findings, actions, intake and run records are kept
  in SQLite.
- **Optional Slack.** A shared channel and a private channel; the place decides
  what a chat may use. Overnight follow-ups, and one morning message per place
  with cards and buttons.
- **Optional Apple integrations.** Reminders (EventKit), Calendar and contacts
  (CalDAV, CardDAV) and iCloud mail (IMAP, read and archive only), each switched
  on by itself.

## Runtime shape

```text
claude.ai / Claude app (chats and scheduled tasks)
       |
       v
Cloudflare Tunnel --> vault connector on 127.0.0.1 (OAuth, /mcp and /mcp/limited)
                         |-- your vault
                         |-- Reminders, Calendar, mail, contacts (each optional)
                         '-- undo store + audit log

Optional agent daemon:
APScheduler / CLI --> Python orchestrator --> Claude runner (Claude Agent SDK)
                            |                    +-- Read / Glob / Grep (vault only)
                            |                    +-- Edit / Write / Agent (only where granted)
                            |                    '-- allowlisted brain tools
                            v
                 SQLite + vault + Slack + configured integrations
```

The connector and the daemon are separate processes; the connector works without
the daemon. The Claude runner is the daemon's only agent runner. Provider
fallbacks and generic shell access are not part of either.

## Quickstart

Run every command in the repo folder. `docs/vault-connector-setup.md` has each
step in full, with what to check after it.

1. **Install.**

   ```bash
   git clone <your-fork-url> brain
   cd brain
   make install
   ```

2. **Your profile.** Copy the example and set your name, time zone and, under
   `notes:`, the folder names you want in the vault. Set these before step 3 so
   the new vault uses the same names.

   ```bash
   install -d -m 700 user
   install -m 600 examples/profile.example.yaml user/profile.yaml
   ```

3. **A vault.** Create one from the templates, or skip this and use an existing
   Obsidian vault. `init-vault` never overwrites a file, so you can also run it
   on an existing vault to add the daily note template the connector needs. The
   connector's daily note is always `<daily_notes_dir>/YYYY/MM/YYYY-MM-DD.md`
   (not Obsidian's default `YYYY-MM-DD.md` at the top of the folder).

   ```bash
   .venv/bin/python -m src.cli init-vault ~/Documents/brain-vault
   ```

4. **Connector settings.** Copy the connector's example file and set
   `vault_path` and `public_url` (`https://<name>.example.com`, on your own
   domain); every Apple integration is off until you switch it on there. The
   connector uses the time zone from your profile (`user.timezone`), or this
   Mac's own zone without it; set `timezone` in this file only to override it.

   ```bash
   install -m 600 examples/vault_connector.example.yaml user/vault_connector.yaml
   ```

   `user/connector_instructions.yaml` is optional: how the rules name you, an
   optional privacy sentence, where each topic's truth lives, and extra rules.
   Without it the connector serves its generic rules. If you start from
   `examples/connector_instructions.example.yaml`, edit or delete its example
   rows first, since everything in the file is sent to claude.ai.

5. **Sign-in.** Set the password (at least 16 characters). `totp-setup` prints
   one `otpauth://` address: add it to your authenticator app as a new account,
   which then shows the six-digit code you sign in with. In Apple's Passwords
   app that is Codes, +, Enter Setup Key, with the letters and digits between
   `secret=` and `&` as the key. Each run of `totp-setup` makes a new secret and
   the old entry stops working, so if a code is refused, run it again and
   replace the entry. Clear the terminal afterwards.

   ```bash
   .venv/bin/python -m src.brain.connector set-password
   .venv/bin/python -m src.brain.connector totp-setup
   ```

6. **Cloudflare Tunnel.** Install `cloudflared`, create a tunnel named `vault`
   (the launchd job runs it by that name), route
   `<name>.example.com` to `http://127.0.0.1:8788`, and add the WAF rule and the
   rate limit, as section 3 of the setup guide describes.

7. **Start the jobs.** Write the launchd jobs with your paths and time zone
   filled in, copy them to `~/Library/LaunchAgents/` and load them from there.
   `render-launchd` prints the same commands with your paths; section 5 of the
   setup guide has more detail. On an Intel Mac, see section 2 of the guide for
   the `cloudflared` path first.

   ```bash
   mkdir -p ~/connector-data/logs ~/Library/LaunchAgents
   export LABEL_PREFIX=com.<name>
   .venv/bin/python -m src.cli render-launchd --label-prefix "$LABEL_PREFIX"
   cp user/launchd/"$LABEL_PREFIX".{vault-connector,cloudflared,connector-health}.plist ~/Library/LaunchAgents/
   for job in vault-connector cloudflared connector-health; do
     launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".$job.plist
   done
   ```

   `render-launchd` also writes two jobs that need no agent daemon:
   `local-backup` commits the vault history and copies the database at 23:50,
   and `garmin-sync` syncs the last seven days of Garmin data at 07:10 when
   `GARMIN_ENABLED` is on. Load them the same way if you use them.

   `curl -s https://<name>.example.com/health` must print `ok`. Then check the
   connector's settings. Without the agent daemon, run only the connector's
   checks; the plain `doctor` also checks the daemon's settings (`.env`,
   `VAULT_PATH` and the like) and fails without them:

   ```bash
   .venv/bin/python -m src.cli doctor --connector
   ```

   On a working install it ends with `PASS — all checks green`, and shows six
   lines: five checks with `✓` and one `i check_connector_integrations (info)`
   line that names the integrations that are on and the time zone in use, for
   example `time zone America/New_York (from profile.yaml)`. A `!` line names
   what is still missing, such as the sign-in or the daily note template.

8. **claude.ai.** In Settings, Connectors, add a custom connector named "Vault"
   with the URL `https://<name>.example.com/mcp`, and a second one named
   "Vault, limited" with `https://<name>.example.com/mcp/limited`. Use only the
   limited one in scheduled tasks. Then set each tool's permission:

   | Tool | Full | Limited | Setting |
   |---|---|---|---|
   | `vault_search`, `vault_read`, `vault_list` | yes | yes | Always allow |
   | `vault_append`, `vault_create`, `open_item_add` | yes | yes | Always allow |
   | `vault_edit` | yes | no | Always allow |
   | `vault_edit_confirm`, `vault_undo` | yes | no | Needs approval |
   | `reminders_list`, `reminder_create` | yes | yes | Always allow |
   | `reminder_update`, `reminder_complete` | yes | no | Needs approval |
   | `calendar_list` | yes | yes | Always allow |
   | `calendar_create`, `calendar_update` | yes | no | Needs approval |
   | `mail_search`, `mail_read`, `contacts_search` | yes | yes | Always allow |
   | `mail_archive` | yes | no | Needs approval |

   "Needs approval" on those seven tools is required: it is the Approve button
   the connector relies on. The Reminders, Calendar, mail and contacts tools
   appear only when their integration is on; with all of them off the connector
   serves 9 tools on `/mcp` and 6 on `/mcp/limited`.

Now ask Claude, in a chat with "Vault" on, to search your vault for a note you
know.

## Optional: the agent daemon and the starter agents

The daemon runs agents on a schedule. The starter kit has a `housekeeping` pass,
the discovery, housing (house listings on two German portals, so it is only
useful in Germany), inbox and humans agents, the `follow_up` run (the night
shift at 23:00), `alfred` (the chat agent of the shared Slack channel) and
`alfred_private` (the chat agent of the owner's private channel and the closing
run that writes the morning messages). Private agents, profiles, source paths and
mode overrides belong under `user/`.

Live runs use the Claude login that Claude Code keeps in the macOS keychain;
sign in once with `claude`. Do not set `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN`: the runner and the doctor refuse to run with either, so
usage stays on the subscription. See [docs/PROVIDERS.md](docs/PROVIDERS.md).

1. Copy a source map and the environment file. `examples/phd_student/` has a
   complete example overlay to start from.

   ```bash
   install -m 600 examples/phd_student/sources.yaml user/sources.yaml
   install -m 600 .env.example .env
   ```

2. Keep `config/integrations.yaml` on `noop` backends for an offline dry run
   (`make demo` runs one against the example overlay).

3. Check the install:

   ```bash
   .venv/bin/brain doctor
   make test
   ```

   Live doctor runs require `VAULT_PATH` in `.env` to be a real directory (not a
   symlink).

4. Run:

   ```bash
   # Inspect a mode without invoking the model
   .venv/bin/python -m src.cli --run scout --dry-run

   # Run it through Claude
   .venv/bin/python -m src.cli --run scout

   # Start the scheduler
   .venv/bin/python -m src.orchestrator.scheduler
   ```

Tracked templates for the scheduler and the encrypted backup job are in `ops/`
(`com.example.tandem.daemon.plist.example` and
`com.example.tandem.backup.plist.example`). Copy each to a local working
file, replace every `__PLACEHOLDER__` with absolute paths, create the log
directory with mode `0700`, and run `plutil -lint` before installing it under
`~/Library/LaunchAgents/`. After a code change, restart the daemon with
`launchctl kickstart -k gui/$(id -u)/com.example.tandem.daemon`. See
[docs/RESTORE_DRILL.md](docs/RESTORE_DRILL.md) for backups and the recovery
drill.

### Add an agent

Create `user/agents/my_agent.md`:

```markdown
---
name: my_agent
model: claude-sonnet-5-5
effort: medium
max_turns: 20
kind: area_owner
identity:
  display_name: "Alfred"
delivery:
  slack:
    channel_id: "C0EXAMPLE99"
builtins: [Read, Glob, Grep]
mcp_tools:
  - mcp__brain-tools__get_recent_findings
  - mcp__brain-tools__store_finding
  - mcp__brain-tools__push_notify
reply_mcp_tools:
  - mcp__brain-tools__reply_to_message
allowed_action_types: [push_notification]
---
{% set your_area = "my_agent" %}
{% include "_preamble.md" %}
{% include "_area_owner.md" %}

{# area-notes #}
What this area covers, where its files are and its rules.
{# /area-notes #}

What the area's run does each morning ...
```

Then add it to `user/modes.yaml`. User files override shipped definitions by
name. A chat in the area reads only the text between the two markers, so write
it to stand alone. See [docs/AGENT_AUTHORING.md](docs/AGENT_AUTHORING.md) for the
full schema.

Available models are `claude-haiku-5-5`, `claude-sonnet-5-5` and
`claude-opus-5-5`. Reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`)
is set separately.

## Optional: Slack

With Slack, the daemon talks to the owner in two places: a shared channel and a
private channel only the owner is in. Every message in the private channel goes
to Alfred; in the shared channel, only messages that name or mention Alfred,
voice notes, and replies in a thread Alfred started or was asked in. The
place decides what a chat may use, so nothing private is processed where others
can see it, and a direct message from anyone but the owner is refused.

At 23:00 the `follow_up` run checks what was asked, promised or left half done,
closes what was handled, finds answers in the owner's own data and prepares
drafts as proposals. In the morning the area runs store findings and proposals,
and the closing run writes one message to each place from them. The private
channel gets one card per thing, with buttons, and the app's Home tab shows what
needs the owner, today, health and open loops.

Create the app from `config/slack_app_manifest.yaml` and follow the Slack section
of [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md). `BRAIN_SLACK_OFF=1` in `.env`
turns Slack off without a code change.

## Optional: Apple integrations

The connector's Reminders, Calendar, mail and contacts tools are switched on one
by one under `integrations:` in `user/vault_connector.yaml`. Calendar, mail and
contacts sign in to iCloud with your Apple ID, set as `ICLOUD_APPLE_ID` in a
`.env` file made from `.env.example` (`install -m 600 .env.example .env`), and an
app-specific password kept in the login Keychain; Reminders use EventKit and need a one-time permission for the launchd
job. Reminder routing (which list and calendar each person or topic uses) is
`reminders.routing` in `user/profile.yaml`. The daemon reads the same
integrations through its brain tools, and can import Garmin metrics when the
`garmin` extra is installed and enabled. See
[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) and section 4 of the setup guide.

## Repository map

| Path | Purpose |
|---|---|
| `src/brain/connector/` | The vault connector |
| `templates/vault/` | The starter vault that `init-vault` copies |
| `deploy/launchd/` | launchd job templates for the connector; `render-launchd` fills them in |
| `examples/` | Example private files to copy into `user/` |
| `config/agents/` | Shipped agent frontmatter and Jinja prompts |
| `config/modes.yaml` | Shipped schedules and execution order |
| `user/` | Your private settings, agents and overrides (gitignored) |
| `src/orchestrator/` | Scheduler, dispatch, chat places and reply routing |
| `src/orchestrator/runners/` | Claude runner, tool-call rules and brain-tool server |
| `src/brain/mcp/` | Brain-tool registry and Python bridge |
| `src/brain/actions/` | Proposals, approval policy, executor and handlers |
| `src/brain/plugins/` | Slack notifier and listener, task backends |
| `ops/` | launchd templates for the daemon and backups |
| `tests/` | Python tests |

## Development

```bash
make test       # pytest
make typecheck  # mypy
make audit      # Python dependency audit
make scan       # personal values in the tracked tree, with your private terms
```

Ruff, the tests, the dependency audit and the personal values scan run in CI.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Safety boundaries

See [SECURITY.md](SECURITY.md) for the trust boundaries and how to report a
problem. In short:

- The connector listens on 127.0.0.1 only, signs in one user with a password
  and a six-digit code, binds each token to its endpoint, and writes nothing to
  rule files, dot folders or anything outside the vault.
- The daemon's sessions have no shell and no generic web tools, load nothing
  from your own Claude setup, and have the daemon's secrets blanked in their
  environment. `Edit`, `Write` and helper agents exist only where an agent's
  file grants them, and direct edits stay inside that agent's own folders.
- World effects go through the action queue. Sensitive effects (drafts to
  send, contact edits, queued vault edits, calendar events with other people)
  wait for a person's approval. A fixed list of reversible types on the owner's
  own devices (creating and completing reminders, creating tasks, and creating
  or changing calendar events without attendees) runs on its own, even in a run
  that has read outside content, and notifications run on their own by
  default. The defaults are in `config/risk_policy.yaml`. An approval is bound
  to the originating area, approval channel, actor, payload hash and action
  version.
- Mail, calendar, contact, web and intake content is untrusted data, not
  instructions.
- `.env`, `user/`, databases, tokens and generated runtime files are never
  committed.

## Documentation

- [Vault connector setup](docs/vault-connector-setup.md)
- [Vault connector design](docs/vault-connector-design.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Agent authoring](docs/AGENT_AUTHORING.md)
- [Integrations](docs/INTEGRATIONS.md)
- [Database](docs/DATABASE.md)
- [Providers and runtime](docs/PROVIDERS.md)
- [Backup and restore drill](docs/RESTORE_DRILL.md)
- [Security](SECURITY.md)

## License

MIT. See [LICENSE](LICENSE).
