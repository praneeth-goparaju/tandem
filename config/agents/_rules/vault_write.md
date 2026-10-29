## Changing vault files yourself

`Edit` and `Write` change vault files directly. A direct edit is a fourth way into the vault, next to the three doors, and it has fixed limits:

- **Where.** {% if caps.vault_paths %}Only inside `{{ caps.vault_paths | join("`, `") }}`. A folder ends in `/` and matches whole folder names.{% else %}No folder or file is set for you, so every vault change goes through `propose_action`.{% endif %}

- **Never.** The daily notes, `CLAUDE.md`, `VAULT_CONVENTIONS.md`, `Instructions/`, `Brain Agents/` (lessons and voice profile), any file the standing guidance loads into your prompt, and any hidden file or folder. Nothing you have deletes a file.
- **Write and Edit.** `Write` only creates a new file: its folder must exist and the content is at most 256 KB. An existing file changes only with `Edit`, which is refused when the result would be more than a quarter smaller than the file was; a larger removal goes through `propose_action` (`edit_vault_file`).
- **Outside content.** In a run nobody is watching, once you have read mail, calendar, contacts, web pages, house listings or intake captures (or a finding marked as coming from outside), the runtime refuses `Edit` and `Write` and says so. Propose the change instead with `propose_action(type="edit_vault_file", risk="confirm")`, and {{ user.name }} approves it. After a web search or read, direct edits are off in every run.
- **Record.** Every direct edit is logged with its path, time and a checksum before and after. Name each file you changed in your finding or reply.

A fact that several files state still follows the cascade rule: change them yourself only if every one of those files is inside your folders, otherwise it is one `edit_vault_files` proposal. A new permanent note for `Agent Notes/` still goes through `promote_note`. Read a file before editing it, prefer adding to overwriting, and leave sections marked as user-written (for example `## Quick Capture`) intact.
