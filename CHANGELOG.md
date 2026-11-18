# Changelog

## Unreleased

- More than one person on one connector: a `people` section in
  `vault_connector.yaml` gives each person their own addresses
  (`/<prefix>/mcp`), password and code, tools, rules, daily notes, allowed
  Reminders lists and calendars, and iCloud account. `set-password`,
  `totp-setup` and `revoke-all` take `--person`. `auth.db` and `undo.db` are
  upgraded in place and existing sign-ins keep working. Held edits are confirmed
  only by the person who caused them, the edit limit is counted per person, and
  open items record `owner` and `assigned_by`. Calendar names given to
  `calendar_create` now match exactly first, and a name that fits more than one
  calendar is refused. Setup: `docs/vault-connector-setup.md`, section 9.
- Connector fixes: open item file names stay within the file system limit;
  hidden files and folders cannot be read; overdue reminders are listed first;
  audit lines are valid JSON and carry the person, the sign-in and the endpoint;
  the hold on edits after a mail read survives token renewals and restarts.
- A nightly change log: `python -m src.brain.connector.change_log` writes one
  note per day with every change made through the connector, by person, and the
  notes that changed outside it. Job template:
  `ops/com.example.tandem.change-log.plist.example`.
- `doctor --connector` renders every person's instructions the way the connector
  does and checks every person's daily note template.

## Release 1: the first public release

- The vault connector: a Streamable HTTP MCP server that gives the Claude app
  on a phone or a browser your Obsidian vault and, optionally, Apple Reminders,
  iCloud Calendar, Mail and Contacts. It runs on your own Mac behind a
  Cloudflare Tunnel, with its own OAuth sign-in (password plus a one-time code
  from an authenticator app), fixed edit rules, an approval step for larger
  edits, an undo store and a monthly audit log. Setup: `docs/vault-connector-setup.md`.
- Everything personal comes from your private folder (`user/`, set by
  `USER_DIR`): the vault layout and time zone from `profile.yaml`, the
  connector's settings from `vault_connector.yaml`, its rules from
  `connector_instructions.yaml`. The shipped files hold placeholders only.
- `init-vault` starts a vault from `templates/vault/`; `render-launchd` writes
  the connector's launchd jobs with your paths; `doctor --connector` checks a
  connector-only install.
- `make scan` checks the tracked files for personal values: public rules for
  everyone, plus your own private terms when `user/private_terms.txt` or
  `tools/pii_blocklist.txt` exists.
- A broken connector install never stops the agent daemon: its doctor checks
  report warnings, even when they crash.
- MIT licence, `SECURITY.md` and `CONTRIBUTING.md`.
