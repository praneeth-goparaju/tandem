# Vault connector: setup guide

This is the one-time setup for the vault connector on the Mac that holds your
vault. The design is in `docs/vault-connector-design.md`.

Type every command in Terminal on that Mac, in the repo folder (the folder you
cloned the `brain` repository into, for example `$HOME/brain`). Type every
password and every code yourself. None of them goes into a file, a chat or a
message to anyone.

In each new Terminal window, first go to the repo folder and point `USER_DIR` at
its `user` folder, so every command below reads and writes the same private
folder that the launchd jobs use (it is gitignored). Replace `$HOME/brain` with
the folder you cloned into:

```bash
cd "$HOME/brain"
export USER_DIR="$PWD/user"
```

The first time, create that folder so only you can read it:

```bash
install -d -m 700 "$USER_DIR"
```

## 1. What gets built

A small web server (the connector) runs on the Mac and listens only on
`127.0.0.1`, so nothing on your network can reach it. A Cloudflare Tunnel
(`cloudflared`) carries traffic from the internet to it. claude.ai talks to the
connector over that tunnel, so your chats and scheduled tasks can read and change
the vault and, when you turn those integrations on, Reminders, Calendar, mail and
contacts.

You need a domain of your own on Cloudflare. Everywhere below, `example.com`
stands for that domain: replace it with yours. You also choose a name for the
address, for example `vault`. Then there are two addresses:

- `https://<name>.example.com/mcp` is the full connector, for your own chats. You
  add it to claude.ai as "Vault".
- `https://<name>.example.com/mcp/limited` is the limited connector, for scheduled
  tasks. It has no tool that edits, undoes, updates, completes or archives. You
  add it as "Vault, limited".

Three launchd jobs keep it running: the connector, the tunnel, and a health check
that runs every 5 minutes and, when the Reminders integration is on, creates a
reminder in your alert list (`Family` unless you change it) when the connector has
failed two checks in a row. A fourth job, a nightly snapshot of the vault into a
local git repository, is optional.

Below, `<name>` means the name you chose. Replace it in every command.

## 2. Prepare the Mac

Install the Cloudflare tunnel program. On an Apple silicon Mac this puts
`cloudflared` in `/opt/homebrew/bin`, the path the tunnel job uses. On an Intel
Mac Homebrew puts it in `/usr/local/bin/cloudflared`; after `render-launchd`
(section 5), change `/opt/homebrew/bin/cloudflared` to that path in the rendered
`cloudflared` job before you copy it.

```bash
brew install cloudflared
```

If the vault is in iCloud Drive, keep it on the disk. In Finder, open iCloud
Drive, right-click the vault folder and choose "Keep Downloaded". Without this,
macOS may hand the connector a placeholder file that is not downloaded yet.

If you have no vault yet, create one from the shipped templates once your
`profile.yaml` is in place (section 4), so the folder names match it:

```bash
./.venv/bin/python -m src.cli init-vault "$HOME/Documents/brain-vault"
```

`init-vault` never overwrites a file; it lists the files it skipped.

Switch off sleep, so the Mac stays reachable. It asks for your Mac password
at the prompt.

```bash
sudo pmset -a sleep 0
```

Make macOS download updates but not install them by itself. Open System Settings,
General, Software Update, click the info button next to Automatic Updates, keep
"Download new updates when available" on, and switch "Install macOS updates" off.
You install updates by hand, at a time you choose.

Make the Claude desktop app open at login. Open System Settings, General, Login
Items, and add Claude under "Open at Login".

Leave FileVault on. After a power cut the Mac waits at the login screen until
someone signs in; see section 8.

## 3. Cloudflare

Your domain must already be a site on Cloudflare, with Cloudflare answering its
DNS. You pick the domain yourself; the records it already has, such as mail
records, are not touched by anything here. You add one new subdomain.

Sign in to Cloudflare from the command line. It opens a browser; choose your
domain there.

```bash
cloudflared tunnel login
```

Create the tunnel. It prints a tunnel ID (a long string of letters, numbers and
dashes) and writes a credentials file `<tunnel-id>.json` to `~/.cloudflared/`. Keep
that file private; do not copy it anywhere.

```bash
cloudflared tunnel create vault
```

Create the file `~/.cloudflared/config.yml` with this content, replacing
`<tunnel-id>` with the ID printed above, `<name>` with your subdomain and
`<home>` with your home folder as `echo "$HOME"` prints it (write the full path;
do not write `$HOME` or `~` in this file):

```yaml
tunnel: <tunnel-id>
credentials-file: <home>/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: <name>.example.com
    service: http://127.0.0.1:8788
  - service: http_status:404
```

The last line answers 404 to any other name, so the tunnel only serves your
subdomain. Port 8788 is the connector's default port.

Point the subdomain at the tunnel. This adds one DNS record.

```bash
cloudflared tunnel route dns vault <name>.example.com
```

Then two rules in the Cloudflare dashboard, which you add by hand
(your domain, Security):

a. A WAF custom rule that keeps strangers away from the tool endpoints. Create a
   custom rule with the action Block and this expression:

   ```
   (starts_with(http.request.uri.path, "/mcp") and not ip.src in {160.79.104.0/21})
   ```

   `160.79.104.0/21` is the address range Anthropic published for claude.ai when
   the design was written. Check Anthropic's current published range before you
   save the rule, and use that one if it differs. The rule is a switch: if
   connector traffic from claude.ai stops arriving (sign-in works but tools fail,
   or the health check is fine and claude.ai still cannot connect), turn this rule
   off first. See section 8. The sign-in pages (`/authorize`, `/token`,
   `/register`, `/login`) are not covered by this rule; they are open on purpose.

b. A rate limiting rule that slows guessing. Match requests whose path is
   `/register`, `/authorize`, `/token` or `/login`, count per IP address, and block
   for a short time when the limit is passed. For example 30 requests per minute.
   If your Cloudflare plan only offers a shorter window, use the closest it allows,
   for example 5 requests per 10 seconds.

## 4. Settings and secrets

All your own values live in three private files in `$USER_DIR`. Each has an
example in `examples/` to start from. None of them is ever committed.

**`profile.yaml`: who you are and how your vault is laid out.**

```bash
install -m 600 examples/profile.example.yaml "$USER_DIR/profile.yaml"
```

Set your name and time zone under `user:`. Two sections matter to the connector:

- `notes:` names the folders in your vault: `daily_notes_dir` (the daily note is
  `<dir>/YYYY/MM/YYYY-MM-DD.md`), `inbox_dir`, `templates_dir`, `actions_dir`
  (where `open_item_add` writes) and `daily_note_template` (inside
  `templates_dir`). Leave a key out to get the default shown in the example. Set
  this before you run `init-vault`, so the new vault uses the same names.
- `reminders.routing` is a free table of rows `{who, list, calendar}`: which
  Reminders list and calendar each person or topic uses. The connector shows it
  to claude.ai as the routing table when the Reminders or Calendar integration is
  on.

**`vault_connector.yaml`: the connector itself.**

```bash
install -m 600 examples/vault_connector.example.yaml "$USER_DIR/vault_connector.yaml"
```

Open `$USER_DIR/vault_connector.yaml` and fill it in. The two values you must set
are `vault_path` (the vault folder on this Mac) and `public_url`
(`https://<name>.example.com`). The other lines are optional: data in
`~/connector-data`, port 8788, the alert list `Family`. The time zone, which
decides today's daily note, open-item ids, reminder times and the alert quiet
hours, is `timezone` in this file when you set it, else `user.timezone` from
`profile.yaml`, else this Mac's own zone; `doctor` prints which one is used.
Under `integrations:` you turn on Reminders, Calendar, mail and contacts one by
one; each one is off until you name its backend there.

**`connector_instructions.yaml`: your part of the rules claude.ai reads.**

The connector sends claude.ai a short set of rules, its server instructions. The
rules are in `config/connector_instructions.md.j2`; how the rules name you
(`owner_name`), an optional privacy sentence (`privacy_rule`), reminder routing
(`routing_rows`), the note that holds the truth for each topic
(`source_of_truth`) and extra rules (`extra_rules`) come from this optional file.
Without the file the connector serves its generic rules, and says so in one
line of its error log at every start. If you start from the
example, edit or delete its example rows before the connector starts, because
everything in the file is sent to claude.ai. When the file has no `routing_rows`, the
routing table is `reminders.routing` in `profile.yaml`, so you can keep it in
one place.

```bash
install -m 600 examples/connector_instructions.example.yaml "$USER_DIR/connector_instructions.yaml"
```

The connector renders the text once at start and writes it to
`~/connector-data/instructions.md`. It refuses to start when the text is longer
than 60 lines or contains an em dash. After a change to this file, restart the
connector.

Create the folder the launchd jobs write their logs to.

```bash
mkdir -p ~/connector-data/logs
```

Only if you turn on the mail, calendar or contacts integration: the tools need
your Apple ID in a `.env` file in the repo folder, and an iCloud app-specific
password (make one at account.apple.com, under Sign-In and Security). Create the
file from the example and set `ICLOUD_APPLE_ID` in it:

```bash
install -m 600 .env.example .env
```

Then keep the password in the login Keychain rather than in `.env`. Type your
Apple ID in place of `<apple-id>`; it must be exactly the same text as
`ICLOUD_APPLE_ID` in `.env`, because the tools look the item up by that name. The
command asks for the password at the prompt; type or paste it there and press
Return. It is not echoed and is never written to a file.

```bash
security add-generic-password -s brain-icloud-app-password -a <apple-id> -w
```

If an item with this name already exists, the command fails. To update the existing
item instead, use this command. `-w` must stay the last option; if anything comes
after it, `security` stores that text as the password and does not ask you.

```bash
security add-generic-password -U -s brain-icloud-app-password -a <apple-id> -w
```

Check that the item is in the Keychain. It must print `found`.

```bash
security find-generic-password -s brain-icloud-app-password -a <apple-id> > /dev/null && echo found
```

Set the sign-in password. The command asks twice. Use a long password of at least
16 characters (a passphrase of four or five words is good). You type it at the
prompt; it is stored only as a hash.

```bash
./.venv/bin/python -m src.brain.connector set-password
```

Set up the six-digit code. The command prints one `otpauth://` address, once. Open
your authenticator app, add a new account by entering that address (or turn it into
a QR code on your own device), and check that the app shows a six-digit code. The
address is never printed again. If you lose it, run this command again and add the
new account; the old one stops working. Once the code is in your authenticator
app, clear the Terminal window with Cmd+K, because Terminal can keep its scrollback
when it restores windows.

```bash
./.venv/bin/python -m src.brain.connector totp-setup
```

`./.venv/bin/python -m src.cli doctor --connector` checks these settings: the
password and the code are set, `vault_path` exists and is not inside the data
folder, `public_url` is https, the instructions render, the daily note template
exists in the vault, and the private folder is readable by you only. It also
prints the integrations that are on and the time zone in use. Without
`--connector`, `doctor` also checks the agent daemon's settings, which a
connector-only install does not have.

## 5. Load the jobs

The launchd jobs are shipped as templates in `deploy/launchd/`. The command
`render-launchd` fills in your folders and writes real files to the `launchd`
folder inside your private folder (`$USER_DIR/launchd/`). It takes the repository
folder from where you run it and your home folder from your account. Your private
folder comes from `USER_DIR`, which you exported at the top of this guide.

Choose the start of the job names once. Use `com.` and a name of your own, for
example `com.jane`. Set it in every Terminal window that runs the commands below:

```bash
export LABEL_PREFIX=com.<name>
```

Render the files. Settings and sign-in data come from the private folder in
`USER_DIR`, and the time zone of the jobs from your settings (section 4). Add
`--vault-path <your vault folder>` to also get the nightly snapshot job.

```bash
./.venv/bin/python -m src.cli render-launchd --label-prefix "$LABEL_PREFIX"
```

The command prints the time zone it used, each file it wrote, and the commands
below with your paths filled in. Copy the three files into
`~/Library/LaunchAgents/`, where launchd looks for them at every login (the
folder can be missing on a new account):

```bash
mkdir -p ~/Library/LaunchAgents
cp "$USER_DIR"/launchd/"$LABEL_PREFIX".{vault-connector,cloudflared,connector-health}.plist ~/Library/LaunchAgents/
```

Load the jobs from there, never from `$USER_DIR/launchd`: a job loaded from
another folder is not loaded again at the next login. If a job is already
loaded, unload it first with `launchctl bootout gui/$(id -u)/<label>`.

Load the connector. It starts at once and again at every login.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".vault-connector.plist
```

Load the tunnel.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".cloudflared.plist
```

Load the health check, which runs every 5 minutes.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".connector-health.plist
```

Check the connector on the Mac itself. It must print `ok`.

```bash
curl -s http://127.0.0.1:8788/health
```

Check the same through the tunnel. It must print `ok`.

```bash
curl -s https://<name>.example.com/health
```

Where the logs are, all in `~/connector-data/logs/`:

- `vault-connector.log` and `vault-connector-error.log`: the connector. Errors and
  warnings are in the error log.
- `cloudflared.log` and `cloudflared-error.log`: the tunnel.
- `connector-health.log` and `connector-health-error.log`: the health check.
- `local-backup.log`: the nightly vault history commit and database copy.
- `garmin-sync.log`: the morning Garmin sync.

### If macOS says "Operation not permitted"

macOS can keep iCloud Drive away from background jobs. If it does, the connector
log (`vault-connector-error.log`) shows only `tool <name> failed: PermissionError`.
The full text, "Operation not permitted" and the path, is in the answer claude.ai
gives and in the `result` field of the audit log (section 8).

In that case, give Python Full Disk Access. The `python` in `.venv` is a link to a
small launcher that starts a Python app inside the Homebrew Python. Print the
location of that app:

```bash
./.venv/bin/python -c 'import sys, os; print(os.path.join(sys.base_prefix, "Resources", "Python.app"))'
```

Open System Settings, Privacy & Security, Full Disk Access, click the plus button,
press Cmd+Shift+G in the file window, paste the path printed above, add it and
switch it on. Then restart the connector:

```bash
launchctl kickstart -k gui/$(id -u)/"$LABEL_PREFIX".vault-connector
```

### Reminders access

With the Reminders integration on, `reminders_list`, `reminder_create`,
`reminder_update`, `reminder_complete` and the alerts from the health check all
use Reminders. macOS decides who may use Reminders by the program that started
the job, so a permission given to Terminal does not count for the launchd jobs.
Do not try to trigger the prompt from Terminal.

After you have loaded the jobs, ask claude.ai (section 6) to list the reminders in
one of your lists, for example `Family`. If it fails with a permission error, open
System Settings, Privacy & Security, Reminders, and switch on "Python" (the entry
macOS added). Then restart the connector:

```bash
launchctl kickstart -k gui/$(id -u)/"$LABEL_PREFIX".vault-connector
```

## 6. claude.ai

In a browser on any device, open claude.ai, Settings, Connectors, "Add custom
connector".

1. Name: `Vault`. URL: `https://<name>.example.com/mcp`. Save, then connect. A
   sign-in page opens on your own address and shows which site is asking. Enter
   the password from `set-password` and the six-digit code from your authenticator
   app.
2. Add a second connector. Name: `Vault, limited`. URL:
   `https://<name>.example.com/mcp/limited`. Sign in the same way. Use this one,
   and only this one, in scheduled tasks.

Then set the permission of each tool of "Vault" in claude.ai (the connector's
tool list). "Always allow" runs without asking. "Needs approval" puts an Approve
button in the chat; only you can tap it, the model cannot.

For the full connector, "Needs approval" on `vault_edit_confirm`, `vault_undo`,
`reminder_update`, `reminder_complete`, `calendar_create`, `calendar_update` and
`mail_archive` is required, not a recommendation. It is the only Approve button in
the system, and the connector relies on it.

The Reminders, calendar, mail and contacts tools are offered only when their
integration is on under `integrations:` in `vault_connector.yaml`. With every
integration off, the connector serves the 9 vault tools on `/mcp` and 6 on
`/mcp/limited`.

| Tool | Full | Limited | Setting |
|---|---|---|---|
| `vault_search` | yes | yes | Always allow |
| `vault_read` | yes | yes | Always allow |
| `vault_list` | yes | yes | Always allow |
| `vault_append` | yes | yes | Always allow |
| `vault_create` | yes | yes | Always allow |
| `vault_edit` | yes | no | Always allow |
| `vault_edit_confirm` | yes | no | Needs approval |
| `vault_undo` | yes | no | Needs approval |
| `open_item_add` | yes | yes | Always allow |
| `reminders_list` | yes | yes | Always allow |
| `reminder_create` | yes | yes | Always allow |
| `reminder_update` | yes | no | Needs approval |
| `reminder_complete` | yes | no | Needs approval |
| `calendar_list` | yes | yes | Always allow |
| `calendar_create` | yes | no | Needs approval |
| `calendar_update` | yes | no | Needs approval |
| `mail_search` | yes | yes | Always allow |
| `mail_read` | yes | yes | Always allow |
| `mail_archive` | yes | no | Needs approval |
| `contacts_search` | yes | yes | Always allow |

For the first test, switch on only the read tools: `vault_search`, `vault_read`,
`vault_list`, and, for the integrations you turned on, `reminders_list`,
`calendar_list`, `mail_search`, `mail_read` and `contacts_search`. Leave every
tool that writes switched off until the checks in section 7 pass.

## 7. Checks

From your phone, in a chat with "Vault" on:

1. Ask Claude to search the vault for a note you know and show you the matching
   lines. The answer must match the vault.
2. Ask it to read that note. The text must match what you see in Obsidian.
3. With the Reminders integration on, ask it to list the open reminders in one of
   your lists. They must match the Reminders app. Without it, ask it to list a
   folder of the vault instead.
4. Open the audit log (section 8). It must have exactly three new lines, one per
   call.
5. With the Reminders integration on: stop the tunnel and wait 11 minutes. During
   this time the health check fails twice and creates one reminder in your alert
   list titled "Vault connector is down". If this falls between 21:30 and 06:00,
   the alarm waits until 06:00 and nothing rings in the night. Then start the
   tunnel again; the next good check clears the count.

   ```bash
   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".cloudflared.plist
   ```

   After the 11 minutes, load it again:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".cloudflared.plist
   ```

Two facts worth writing down at the first sign-in, in case you need to debug it:

- Whether claude.ai sent a `resource` with its sign-in request. The connector
  accepts a sign-in only for its own two addresses, so if sign-in works at all,
  claude.ai sent one. If sign-in fails with a message that the resource must be
  one of the two addresses, write down the exact message claude.ai shows.
- Whether requests carry an `Mcp-Session-Id` header. The normal logs do not show
  it. To see it, stop the connector job, then run the connector by hand with more
  logging, make one call from your phone, and look at the output. Make the call
  with a vault tool such as `vault_list`, not a mail or Reminders tool, because
  macOS gave Reminders access to the launchd job, not to the Terminal (section 5):

  ```bash
  launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".vault-connector.plist
  ```

  ```bash
  ./.venv/bin/python -c "import logging, sys; logging.basicConfig(level=logging.INFO); from src.brain.connector.__main__ import main; sys.exit(main(['serve']))"
  ```

  A line "Created new transport with session ID" means claude.ai opened a session
  and the library handed out an id; a line "Rejected request with unknown or
  expired session ID" means a request came with an id the connector did not know.
  If neither line appears, claude.ai is not using sessions. Stop the run with
  Ctrl+C and load the job again:

  ```bash
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$LABEL_PREFIX".vault-connector.plist
  ```

## 8. Day to day

Sign every client out (claude.ai, and anything else that signed in). This cancels
every token and code. You sign in again from claude.ai afterwards.

```bash
./.venv/bin/python -m src.brain.connector revoke-all
```

Read the audit log. It is one file per month in `~/connector-data/audit/`, one JSON
line per tool call, with the time (UTC), the tool, what it acted on, the result and
a short token id. Show the last 50 lines of this month's file:

```bash
tail -n 50 ~/connector-data/audit/audit-$(date -u +%Y-%m).jsonl
```

Files older than 12 months are removed by the connector. Note and passage text is
not in the log; only its length.

Restart the connector after changing `vault_connector.yaml`, `profile.yaml` or
`connector_instructions.yaml`:

```bash
launchctl kickstart -k gui/$(id -u)/"$LABEL_PREFIX".vault-connector
```

After a power cut, the Mac restarts and waits at the FileVault login screen.
Nothing runs until someone signs in on the Mac itself. After you sign in, the
launchd jobs and the Claude app start by themselves. If you cannot get to the
Mac, the connector stays down; the health check cannot alert you because it runs
on the same Mac.

To turn the Cloudflare IP rule off, open the Cloudflare dashboard, your domain,
Security, WAF, custom rules, and switch the rule from section 3a off (do not delete
it). Do this if connector traffic from claude.ai stops arriving. The login and
rate limits stay in place. Switch the rule on again once you know claude.ai's
current address range.
