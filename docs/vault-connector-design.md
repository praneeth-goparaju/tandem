# Vault connector: design

**Status:** built and in use. The setup steps are in `docs/vault-connector-setup.md`;
this document explains why the connector works the way it does.

## 1. Goal

From a normal Claude chat, on the phone app or claude.ai on the web, the owner can
search, read and add to their Obsidian vault, and, when they switch those integrations
on, manage Apple Reminders and iCloud Calendar and read iCloud Mail and Contacts. The
vault stays on the owner's own Mac; nothing is copied to a server.

## 2. Decisions

| Topic | Decision |
|---|---|
| Host | An always-on Mac at home. The author uses Apple silicon; an Intel Mac needs only the `cloudflared` path change in the setup guide. |
| Public address | A subdomain of the owner's own domain on Cloudflare, written `<name>.example.com` here, reached through a Cloudflare Tunnel. Nothing is opened on the home router. |
| Users | Only the owner signs in. |
| Scope | A fixed set of tools. No tool runs commands. |
| Never | No tool deletes or moves a note, deletes a reminder or event, or sends mail or messages. No tool replaces or empties a note without the owner's approval, which Claude asks for with an Approve/Skip question card, and every edit can be undone for 90 days. |
| Approvals | claude.ai's per-tool setting "Needs approval" puts an Approve button in chat. The owner taps it; the model cannot. |
| Edits | Small edits run without a tap; larger or sensitive ones wait for the owner's approval, which Claude asks for with an Approve/Skip question card (section 4). |

Deployment values (hostname, vault path) are in the private `user/vault_connector.yaml`;
the password hash, the TOTP secret, tokens, the undo store and the audit log are in the
connector's data folder.

## 3. The connector

### 3.1 Hosting

- New package `src/brain/connector/`, own entry point (`python -m src.brain.connector`),
  separate process from everything else.
- Official `mcp` Python SDK, Streamable HTTP, listening on 127.0.0.1 only; the Cloudflare
  Tunnel is the only way in. `cloudflared` and the connector are launchd jobs that start
  at login and restart on crash.

### 3.2 Login

1. **OAuth 2.1 with PKCE**, using the SDK's authorization-server routes and our own
   provider. One user: a long password (argon2 hash) plus a 6-digit TOTP code.
2. **Phishing guard.** Dynamic client registration is accepted only for claude.ai's
   published OAuth callback URLs (claude.ai and claude.com by default, `allowed_redirect_uris`
   in `vault_connector.yaml`); any other `redirect_uri` is refused. The sign-in page
   shows which site is asking.
3. **Tokens bound to their endpoint.** Each token records the endpoint (full or limited)
   it was issued for (RFC 8707 resource indicator) and is refused anywhere else. Access
   tokens last 1 hour, refresh tokens 30 days, rotated on use, stored hashed in SQLite.
   `python -m src.brain.connector revoke-all` cancels every token.
4. **Failed logins** are slowed per address (doubling delay), not locked globally, so a
   stranger cannot lock the owner out. The owner is alerted after 10 failures in an hour.
5. **Cloudflare filter (second layer).** The two MCP paths accept only Anthropic's
   published egress range (160.79.104.0/21 when written); the sign-in pages are open.
   It is a switch, because one public report says connector traffic arrived from other
   addresses.

### 3.3 Two endpoints

- **Full** (`/mcp`): for the owner's own chats. Added at claude.ai as "Vault".
- **Limited** (`/mcp/limited`): for chats that run unattended, such as scheduled tasks
  in the Claude app, because they read text written by strangers. Added as "Vault, limited". No edit,
  undo, update, complete or archive tools.

### 3.4 Tools

"Setting" is the recommended claude.ai permission for the full connector. The
Reminders, Calendar, mail and contacts tools are offered only when their integration is
switched on in `vault_connector.yaml`; with all of them off the connector serves the 9
vault tools on `/mcp` and 6 on `/mcp/limited`.

| Tool | What it does | Full | Limited | Setting |
|---|---|---|---|---|
| `vault_search` | Search words, folder or frontmatter; returns matching lines, capped. | yes | yes | Always allow |
| `vault_read` | Read a note or a line range; returns a content hash. | yes | yes | Always allow |
| `vault_list` | List a folder. | yes | yes | Always allow |
| `vault_append` | Add to the end of a note or under a heading; creates today's daily note from the template, filling its `{{date}}` fields. | yes | yes | Always allow |
| `vault_create` | New note; refused if the name exists anywhere in the vault. | yes | yes | Always allow |
| `vault_edit` | Replace one exact passage (section 4). | yes | no | Always allow |
| `vault_edit_confirm` | Run an edit that section 4 held. | yes | no | Needs approval |
| `vault_undo` | Restore what an edit replaced. | yes | no | Needs approval |
| `open_item_add` | Create an obligation note in `Actions/` following the open-item format (`A-YYYYMMDD-NN - Outcome.md`, status `next` or `waiting`). | yes | yes | Always allow |
| `reminders_list` | Open reminders in any list. | yes | yes | Always allow |
| `reminder_create` | Create a reminder; a due date always gets an alarm. Routed per the owner's routing table. | yes | yes | Always allow |
| `reminder_update` | Change title, notes, date or alarm (new EventKit handler). | yes | no | Needs approval |
| `reminder_complete` | Mark done (new EventKit handler). | yes | no | Needs approval |
| `calendar_list` | Events in a date range. | yes | yes | Always allow |
| `calendar_create` | Event with an alert, default 60 minutes before. | yes | no | Needs approval |
| `calendar_update` | Change time, title, place or alert. | yes | no | Needs approval |
| `mail_search`, `mail_read` | iCloud mail, read only. | yes | yes | Always allow |
| `mail_archive` | Move a message to Archive. | yes | no | Needs approval |
| `contacts_search` | Look up a contact. | yes | yes | Always allow |

**Removed after review:** the tools for the old daemon's approval queue
(`pending_actions`, `approve_action`, `reject_action`). The executor would not run
connector approvals anyway (it only accepts its own approval channel), and the queue
could hold message-sending actions. An unattended chat that wants something risky done
creates an open item in `Actions/` instead; the owner does it in their own chat.

**Never offered:** deleting or moving notes, replacing or emptying a note without the
owner's approval (which Claude asks for with an Approve/Skip question card), deleting reminders or events, sending anything, running commands.

### 3.5 Rules enforced in code

1. **Protected paths for every write tool, not only edits:** `CLAUDE.md`, `Instructions/`,
   `Brain Agents/`, `.claude/`, `.obsidian/`, `.trash/`, and anything outside the vault.
   Paths are checked after resolving symlinks. Instruction and settings files are
   protected wherever they are in the vault: any part of the path named `CLAUDE.md`,
   `CLAUDE.local.md`, `AGENTS.md`, `.mcp.json`, `.claude`, `.codex`, `.obsidian`,
   `.trash`, `Instructions` or `Brain Agents` (ignoring case), and any part that starts
   with a dot. `vault_create` makes only `.md` notes, never a name that starts with a dot
   or ends in `.icloud`.
2. **`01 - Daily Notes/Action Queue.md` is owner-only** (in the daily notes folder the
   profile names). Agents never write it; open items go to `Actions/`.
3. **Read-back on every write.** The tool re-reads the file and returns what landed; a
   mismatch is an error, never "done".
4. **Every write checks for a newer version.** Append and create also compare the file's
   modification time and hash just before writing, and use macOS file coordination, so an
   edit from the phone that iCloud has not finished delivering is never overwritten. A
   vault in iCloud Drive (optional) is set to "Keep Downloaded" so files are never offloaded.
5. **The owner's privacy rule.** The server instructions can carry one sentence of the
   owner's own privacy rule (for example, what must never go into shared vault files).
   It is the optional `privacy_rule` in `user/connector_instructions.yaml`.
6. **Audit log**, one JSON line per call (time, tool, target, result, token), rotated
   monthly, kept 12 months.
7. **Server instructions** come from a fixed template in the repository,
   `config/connector_instructions.md.j2`, rendered once at start with the private
   `user/connector_instructions.yaml` and the routing table in `user/profile.yaml`. They
   are never generated from the live vault, so an injected note cannot plant
   instructions.

### 3.6 Mail, calendar and contacts access

Calendar and contacts use CalDAV and CardDAV with the iCloud app-specific password;
reminders use EventKit. That password also allows sending mail through SMTP, so the
connector has no SMTP code. The password is read from `.env` (`ICLOUD_APP_PASSWORD`) or,
when that is empty, from the login Keychain item `brain-icloud-app-password` under the
account `ICLOUD_APPLE_ID`.

## 4. Edits without tapping

claude.ai only allows Always allow, Needs approval or Blocked per tool, so the judgement
sits in the connector. **Version 1 uses fixed rules only**; the review showed that a
second-model check could itself be manipulated by the text it judges, and would often be
too slow to answer in time.

1. Every `vault_edit` needs the content hash from a `vault_read` of that note, and a one-
   sentence `reason`.
2. **Runs without a tap** when all of these hold: it replaces at most 10 lines, removes no
   more than a quarter of the note, does not touch a heading, does not change frontmatter
   other than `status` in `Actions/` notes, is outside the protected paths, and fewer than
   20 edits have run in the last hour.
3. **Held for the owner's approval** (Claude asks with an Approve/Skip question card,
   then calls `vault_edit_confirm`) otherwise, and also for every edit
   in a chat that has read mail in the last 30 minutes, because that is where planted
   instructions come from.
4. **Undo.** The replaced text goes to a separate undo store (owner-only file permissions,
   kept 90 days), not the audit log. `vault_undo` restores it after the same hash check.

If fixed rules hold too many ordinary edits in practice, a model-based check can be added
later, as its own change.

## 5. Alerts from the Mac

The connector's own alerts (the connector down for two health checks in a row, repeated
failed sign-ins) are created as an Apple Reminders item with an alarm a minute ahead, in
the list named by `alert_lists.household` in `vault_connector.yaml`. Reminders
notifications reliably reach the phone, and the item stays visible until it is ticked
off. Between 21:30 and 06:00 in the connector's time zone, alerts wait until 06:00.

## 6. Mac operations

- **Logged-in user required.** A vault in iCloud Drive, the login Keychain, launchd agents
  and the Claude app all need the owner's user logged in. With FileVault on, a power cut stops at
  the login screen until someone logs in; the health check then alerts the owner.
  FileVault stays on.
- `cloudflared` and the connector start at login. macOS updates are installed by hand,
  not automatically overnight.
- Sleep is switched off (`pmset`). These are the setup guide's recommendations for a Mac
  that stays reachable, not requirements of the code.

## 7. Testing

1. **Unit tests** on a small fixture vault: protected paths on every write tool, Action
   Queue refused, read-back, newer-version refusal for append and edit, create refused on
   an existing name, edit rules (each limit and the mail-read hold), undo, template dates.
2. **Login tests:** no token, expired, revoked, wrong TOTP, a limited token on the full
   endpoint, and a foreign `redirect_uri` all fail.
3. **Live tests from the phone:** search; append to today's note; a reminder created
   and completed; a calendar event with Approve; a small edit without a tap; a large edit
   with Approve; undo; then the audit log shows every call.

## 8. Risks

- **Planted instructions in mail, web pages or transcripts.** Unattended chats use the
  limited endpoint, with no edit tools. In the owner's
  chats, edits after reading mail are held, all writes avoid protected paths, server
  instructions come from the repo, and every edit can be undone. A small planted edit in a
  chat that has not read mail could still pass; the undo store and audit log are the answer.
- **iCloud sync.** Newer-version checks and file coordination turn conflicts into refusals.
- **The Mac down.** Chat reports the connector unreachable; the health check alerts the
  owner; a daily snapshot of the vault (`scripts/vault_snapshot.sh`) protects it.
- **Anthropic changes its addresses.** The Cloudflare filter is a switch; the login stands
  alone.
