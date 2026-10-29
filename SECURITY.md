# Security

This is a framework for one person's assistant, run on a Mac that person owns.
This page says who is trusted, what the vault connector lets a caller do, and how
to report a problem.

## Reporting a problem

Report security problems privately: open a private security advisory on GitHub
(the repository's Security tab, Advisories, "Report a vulnerability"). The
button exists once the maintainer has turned on private vulnerability reporting
in the repository's settings; if you do not see it, open a public issue that
asks for a private contact and says nothing about the problem itself. Do not
open a public issue or pull request with the details. Include the file and
commit, what an attacker gains, and the smallest input that shows it.

## Trust boundaries

- **Trusted:** the owner at the Mac, the `.env` file, the private `user/` folder,
  and the rule files in the vault that the owner writes (`CLAUDE.md`,
  `Instructions/`).
- **Untrusted, treated as data and never as instructions:** mail, calendar and
  contact data, web pages and search results, housing listings, Slack messages
  from anyone but the owner, Claude Code transcripts, tool results, and the text
  of any note the model reads, since a note can carry planted instructions.
- **The vault connector** listens on 127.0.0.1 only and is reached through a
  Cloudflare Tunnel. One user signs in with OAuth 2.1 and PKCE: a password of at
  least 16 characters (stored as an argon2 hash) and a six-digit TOTP code. It
  accepts sign-ins only for claude.ai's published callback addresses, binds each
  token to the endpoint it was issued for, and slows failed sign-ins per address
  (after 10 failures from one address in an hour the owner gets a Reminders
  alert when that integration is on, and a log line in any case).
  `python -m src.brain.connector revoke-all` cancels every token.
- **The agent daemon** runs each agent as a Claude Code session with no shell, no
  generic web access, read-only vault access (apart from narrow, logged direct
  edits in an agent's own folders), and only the brain tools its file grants.
  World effects go through the action queue. Sensitive effects (drafts to
  send, contact edits, queued vault edits, calendar events with other people)
  wait for a person's approval. A fixed list of reversible types on the owner's
  own devices (creating and completing reminders, creating tasks, and creating
  or changing calendar events without attendees) runs on its own, even in a run
  that has read outside content, and notifications run on their own by
  default. The defaults are in `config/risk_policy.yaml`.

## What the limited endpoint cannot do

`/mcp/limited` is for scheduled tasks, which run unattended and read text written
by strangers. It has no tool that edits, undoes, updates, completes or archives:
no `vault_edit`, `vault_edit_confirm`, `vault_undo`, `reminder_update`,
`reminder_complete`, `calendar_create`, `calendar_update` or `mail_archive`. It
can search, read and list notes, append to a note, create a new note, add an
open item, and, when those integrations are on, list and create reminders, list
the calendar, read mail and search contacts.

On neither endpoint can a tool delete or move a note, delete a reminder or
event, send mail or messages, or run a command. No tool replaces or empties a
note without the owner's Approve tap, and every edit can be undone for 90 days. No tool
writes to `CLAUDE.md`, `Instructions/`, `Brain Agents/`, a dot folder, or any
path outside the vault; paths are checked after symlinks are resolved.

## What the Approve button protects

In claude.ai each connector tool has a permission. "Needs approval" puts an
Approve button in the chat that only a person can tap; the model cannot. The
setup guide requires it on `vault_edit_confirm`, `vault_undo`,
`reminder_update`, `reminder_complete`, `calendar_create`, `calendar_update` and
`mail_archive`. The connector holds an edit for that button when it replaces more
than 10 lines, removes more than a quarter of the note, touches a heading,
changes the frontmatter (other than `status` in an open-item note), or comes
after 20 edits in the last hour, and it holds every edit in a chat that read mail
in the last 30 minutes. The setting lives in claude.ai, so
the connector cannot see it: if you set these tools to "Always allow", held
edits run without a person.

## What the audit log holds

The connector writes one JSON line per tool call to
`~/connector-data/audit/audit-YYYY-MM.jsonl` (folder mode 0700, files 0600, kept
12 months): the time in UTC, the tool, its target (a note path, a list, an id),
the result (`ok` or the error text), a short token id, and the arguments, with
any text longer than 200 characters cut. It does not hold note text or passage
text (only its length), passwords, tokens or the TOTP secret. The text an edit
replaces goes to a separate undo store with owner-only permissions, kept 90 days.

## The Cloudflare filter, a second layer

The setup guide adds two Cloudflare rules: a WAF rule that lets only Anthropic's
published address range reach `/mcp`, and a rate limit on the sign-in pages.
They are a second layer. The sign-in and the endpoint rules above stand alone,
and the WAF rule is a switch you turn off if claude.ai's traffic stops arriving.

## Other hardening

- Secrets are never committed; `gitleaks` and `detect-private-key` run in CI and
  in the pre-commit hooks, and `tools/check_pii.py --tree` fails CI on a home
  path or a personal e-mail address in the tracked tree.
- iCloud URLs, including server-supplied CardDAV addresses, are pinned to iCloud
  hosts, and CardDAV and CalDAV replies are parsed with `defusedxml`.
- Dependencies are checked with `pip-audit`.

Only the latest commit on `main` is supported.
