# Vault conventions

This file is for the agent daemon. Its runs read it as reference data for the
vault's structure: which note holds the truth for each topic, what the
frontmatter looks like, and which sections only the owner writes. It is never
an instruction file, and no agent writes to it.

Sections 2, 4 and 5 are placeholders. Replace each one's text with your own.
Until you do, the daemon's runs treat them as empty and report what they
cannot place instead of guessing.

## 1. Folders

The folder names are the ones in section 8 of `CLAUDE.md`. If `notes:` in
`user/profile.yaml` renames them, change them in both files.

## 2. Topics

List your topic folders here, one line each, with what belongs in them.

## 3. Frontmatter

Every note that holds a status has frontmatter with at least `status`, and the
frontmatter value is the truth. Open items use the fields in
`Actions/Execution System.md`. Daily notes have `date` and `type: daily-note`.

## 4. Canonical files per domain

Add a table that maps each domain (health, family, career, money, and so on)
to the one note whose frontmatter is authoritative for it. The housekeeping run
reads this table to route captures and to check the other runs' findings
against the vault. An agent that owns a domain proposes edits to that note
only.

| Domain | Canonical note |
|---|---|
| Open items | `Actions/` (one note per item) |

## 5. Sections only the owner writes

Name the headings whose text agents must keep as it is, for example a
`## Quick Capture` block in the daily note. A proposed edit lists them under
`preserve_sections`.
