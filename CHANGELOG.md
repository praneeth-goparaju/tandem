# Changelog

## Unreleased

Nothing yet.

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
