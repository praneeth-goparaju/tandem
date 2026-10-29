# Agent authoring

Agents are Markdown files with YAML frontmatter. Shipped agents live in
`config/agents/`; private additions and overrides live in `user/agents/`.
Every agent speaks as Alfred, so every file carries the same `identity`.

## Minimal agent

```markdown
---
name: career
model: claude-sonnet-5-5
effort: medium
max_turns: 20
kind: area_owner
builtins:
  - Read
  - Glob
  - Grep
mcp_tools:
  - mcp__brain-tools__get_recent_findings
  - mcp__brain-tools__store_finding
identity:
  display_name: "Alfred"
delivery:
  slack:
    channel_id: "C0123456789"
---
{% set your_area = "career" %}
{% include "_preamble.md" %}
{% include "_area_owner.md" %}

{# area-notes #}
Career: what the area covers, where its files are and its rules.
{# /area-notes #}

What the career run does each morning ...
```

The loader validates each file at startup. A file beginning with `_` is a
shared Jinja fragment, not an agent.

## Frontmatter

| Field | Type/default | Meaning |
|---|---|---|
| `name` | required string | Stable lowercase identifier; filename stem is the default |
| `model` | `claude-sonnet-5-5` | `claude-haiku-5-5`, `claude-sonnet-5-5`, or `claude-opus-5-5` |
| `effort` | `medium` (runner default) | `low`, `medium`, `high`, `xhigh`, or `max` |
| `max_turns` | `30` | Model turns, 1–100 |
| `description` | empty | Human-facing summary |
| `kind` | optional | `area_owner`, `service`, or `system` |
| `builtins` | empty | Explicit Claude Code built-ins: `Read`, `Glob`, `Grep` (limited to the vault), and the gated `Edit`, `Write`, `Agent` |
| `mcp_tools` | empty | Explicit full `mcp__brain-tools__...` names |
| `reply_mcp_tools` | empty | Additive tools exposed only during an authenticated inbound reply |
| `allowed_action_types` | empty | Explicit action types this agent may queue through `propose_action` or typed wrappers |
| `identity` | optional | Slack display name/icon; every agent uses `Alfred` and one icon |
| `delivery.slack.channel_id` | optional | Slack destination for this agent: its place |
| `chat` | optional | `shared` or `private`: the agent that holds the chat of its channel's place |
| `vault_paths` | empty | Vault folders (ending in `/`) and files this agent may edit directly, and what a shared-place chat may read |
| `max_helpers` | `0` | Helpers a run may start, 0–3; needs `Agent` in `builtins` |
| `before` | optional | A dotted module name under `src`. The executor runs `python -I -m <module>` as a plain process, in the repository root, right before the agent's session |
| `before` | optional | A module under `src` (for example `src.brain.media.expire`) that runs as a plain process right before this agent's session |
| `reply_brief` | empty | Extra instructions used only for inbound Slack replies |

Agents inherit no tools, so grant only the smallest set needed for the role.
`Bash`, `WebSearch`, `WebFetch`, `MultiEdit`, `NotebookEdit` and `Skill` are
denied for every run, and the loader rejects them in `builtins`. `Edit`,
`Write` and `Agent` are real grants with fixed limits, described under
Direct edits and helpers below.

Do not grant agents external search. Public discovery uses the deterministic
`src.orchestrator.public_discovery` code job, whose outbound requests come only
from the explicitly public query catalog. Its results enter intake quarantine;
the networkless discovery agent can read them through its narrow candidate tool.

When `delivery.slack` is absent, the registry removes `push_notify` and
`reply_to_message` even if they are declared.

A `before` module gets 120 seconds, `PATH` and the variables `HOME`, `USER_DIR`,
`BRAIN_VERIFY_RUN`, `MEMORY_DB_PATH`, `VAULT_DB_PATH`, `VAULT_PATH` and
`BRAIN_SLACK_OFF` when the daemon has them, and nothing else of the daemon's
environment; a module that imports `src.db` then loads the repository `.env`
itself, as every brain process does. Its exit code does not stop the session: a
non-zero code or a timeout is logged, with the last 500 characters of its stderr,
and the session runs. It does not run
while the usage limit holds, since the session is skipped then. The `follow_up`
agent uses it for the follow-up collector, whose cursor the executor commits
only after the collector exited 0 and the session succeeded.

Put conversational mutations and `reply_to_message` in `reply_mcp_tools`, not
`mcp_tools`. The reply router adds them only after authenticating the inbound
sender and marks the tool context as a reply; scheduled runs never receive
them. Keep proactive actions in the action queue.

## Places and chat

An area's place is the Slack channel it is delivered to. There are two places,
the shared channel and the owner's direct messages, and exactly one agent per
place sets `chat` (`shared` or `private`). An agent without Slack delivery, a
`kind: system` agent such as `housekeeping`, belongs to the private place: its
approval cards and its morning item go to the direct messages. Every Slack
message goes to the place's chat agent, and `chat.py` builds the run from the
union of what the place's areas are granted: `tools`, `reply_mcp_tools`,
`allowed_action_types` and `vault_paths`. The shared place's areas are the
agents delivered to the shared channel; the private place's areas are every
registered agent. An agent delivered to the shared channel therefore shares all
four with the co-owner's chats, and the shared place drops the mail, contacts,
`search_knowledge` and `ops_summary` tools and the `mail_archive`,
`mail_archive_batch`, `send_draft`, `create_contact`, `update_contact` and `promote_intake` action
types whatever the areas grant. The shared place reads only its areas'
`vault_paths`, so with none listed it reads no vault file, and it may propose
`edit_vault_file`, `edit_vault_files` and `insert_vault_line` only for paths
inside them. Moving an area to the other place means changing its
`delivery.slack.channel_id`, or its entry under `notifier.options.channels` in
the private overlay. The starter kit's areas ship in the private place; map one
to the shared channel in the private overlay to share it. A channel with no
chat agent is not a place: a message there is answered with “This channel is
not set up for Alfred yet.”

## Area notes

An area's file has two parts. The **area notes** say what Alfred knows and
cares about in the area: what it covers, where its files are, its rules. The
**morning checklist** is what the area's run does each morning and is used only
by unattended runs. Put the notes between two Jinja comments:

```text
{# area-notes #}
...
{# /area-notes #}
```

The comments vanish when the whole prompt renders, so the notes stay part of
the area's own run. A chat gets only the block: the `area_notes` tool returns
it for any area of the place, and a reply in an area's thread puts it in the
prompt. Write it to stand alone. The doctor fails a `kind: area_owner` agent
that lacks either marker.

## Direct edits and helpers

`Edit` and `Write` let a run change vault files itself. They work only inside
the agent's `vault_paths`: a folder ends in `/` and matches whole folder names,
and an agent with `Edit` or `Write` but no `vault_paths` can edit nothing. Keep
`vault_paths` clear of the daily notes, `Brain Agents/`, `Instructions/`, the
vault's rule files and every file the standing guidance loads into a prompt:
the doctor fails an entry that is, lies inside, or contains one of them, so a
folder that holds such a file has to be narrowed to its sub-folders. `Write`
creates files and `Edit` changes existing ones; an unattended run that has read
outside content loses both and proposes an `edit_vault_file` instead. The
full rule set is in [ARCHITECTURE.md](ARCHITECTURE.md).

`Agent` lets a run start read-only helpers, at most `max_helpers` of them
(0 to 3). With `max_helpers: 0` the runner drops `Agent` even when it is
listed.

`BRAIN_DIRECT_EDITS=0` and `BRAIN_HELPERS=0` in `.env` turn the two features off
for every agent once the daemon is restarted. The prompt follows: `caps` tells
the preamble whether to include `_rules/vault_write.md` or
`_rules/vault_read_only.md` and `_rules/helpers.md`, so a prompt never
describes a tool the session lacks.

## Before steps

`before: src.<module>` names a Python module that the executor runs first, as
`python -I -m <module>` in the repository root, right before the agent's session.
It is for deterministic work that needs no model: `housekeeping` uses
`src.brain.media.expire` to delete media older than 30 days. The name must start
with `src.` and use lowercase letters, digits, dots and underscores; the loader
rejects anything else. The process is not a Claude session. It gets a 120-second
timeout and an environment of `PATH` plus `HOME`, `USER_DIR`, `BRAIN_VERIFY_RUN`,
`MEMORY_DB_PATH`, `VAULT_DB_PATH` and `VAULT_PATH` when they are set, and nothing
else of the daemon's environment. A non-zero exit, or a timeout, is logged and the
session still runs, so write a before step that is safe to run twice and safe to
skip. The step starts only after the checks the session itself makes (no API
credential; in a verification run, copies of the database, the vault and
`USER_DIR`), so in a verification run it works on the copies.

## Pictures

`draw_chart` and `draw_image` draw a picture and return a media id.
`reply_to_message` and `push_notify` take up to four ids in `media_ids`, and
each `send_morning_cards` card takes `media_ids` too. The pictures are posted
under the message's text, in the same channel and thread. Grant the draw tools in `mcp_tools`, not only in
`reply_mcp_tools`, so scheduled runs such as the closing run's morning message
have them too. The shipped grants are `alfred` and `alfred_private`; give them
to another agent only when it has numbers or a diagram worth showing.

- `draw_chart(spec)` takes numbers and labels only, never anything to evaluate:

  ```json
  {"title": "Steps per day", "kind": "bar", "x": ["Mon", "Tue", "Wed"],
   "series": [{"name": "steps", "values": [8200, 9100, 7600]}], "y_label": "steps"}
  ```

  `kind` is `line` or `bar`, `x` has 1 to 366 labels, and `series` has 1 to 6
  entries with one number for each label. A spec with a value short, a number
  given as text, `NaN` or infinity is refused with the reason, and nothing is
  drawn.
- `draw_image(svg)` takes SVG text of at most 512 KB whose root has a width and
  height or a viewBox of at most 4096 by 4096. A document with a script, a
  `foreignObject`, an event handler, a DOCTYPE, an `href` that is not a `#id` or
  a `data:` URL holding a PNG, JPEG, GIF or WebP picture (no SVG inside an SVG),
  or `url(` in a style or an `href` is refused as a whole.
- A media id works only in the place it was made for, so draw in the run that
  posts. The tool's answer names an id the notifier left out.
- `BRAIN_MEDIA=0` in `.env` turns both tools off, and voice notes with them, once
  the daemon is restarted. The architecture notes cover the whole path:
  [ARCHITECTURE.md](ARCHITECTURE.md).

## Template context

The body uses Jinja2 with strict undefined variables. Common values are:

| Expression | Source |
|---|---|
| `{{ user.name }}` | `user/profile.yaml` |
| `{{ family... }}` | `user/profile.yaml` |
| `{{ today }}`, `{{ weekday }}`, `{{ now }}` | runtime clock |
| `{{ today_year }}`, `{{ today_month }}` | runtime clock |
| `{{ source("career") }}` | `user/sources.yaml` |
| `{{ active_mode }}` | current mode |
| `{{ expected_agents }}` | agents dispatched by the mode |
| `{{ expected_area_owners }}` | dispatched agents with `kind: area_owner` |
| `{{ caps }}` | what this run may do: `caps.direct_edits` (bool), `caps.helpers` (how many helpers it may start) and `caps.vault_paths` (the folders and files it may edit) |

Unknown variables fail rendering rather than becoming empty strings.

Shared fragments include:

- `_preamble.md`: trust boundary, the two places, operating rules, and Slack
  formatting;
- `_area_owner.md`: ledger/action responsibilities for an area owner;
- `_icloud_surface.md`: mail/calendar/reminder/contact behavior;
- `_rules/vault_write.md`: the direct-edit rules, included when
  `caps.direct_edits`;
- `_rules/vault_read_only.md`: the vault mutation contract of a run without
  direct edits;
- `_rules/helpers.md`: the helper rules, included when the run may start
  helpers (`caps.helpers`).

## Tool selection

The Python registry resolves an effective list. `ClaudeRunner` splits it into:

- built-ins: Read, Glob, Grep, and Edit, Write, Agent when granted;
- brain tools: the suffix of `mcp__brain-tools__<name>`.

The Claude runner offers only those tools, and the Python bridge checks the
same allowlist again before invocation. Tool handlers also receive immutable
caller context (`agent`, `area`, `allowed_action_types`, whether the run has
received outside content, and for a chat the `areas` it is limited to) for
authorization and audit. Action capabilities are a separate default-deny
boundary: granting `propose_action` does not grant every action type, and typed
wrappers such as `push_notify` require their corresponding action type too.
Doctor rejects unknown/duplicate action types and missing wrapper capabilities.

Examples:

```yaml
# Read-only analyst
builtins: [Read, Glob, Grep]
mcp_tools:
  - mcp__brain-tools__get_recent_findings

# Agent that can record findings and propose, but not approve, actions
builtins: [Read, Glob, Grep]
mcp_tools:
  - mcp__brain-tools__get_recent_findings
  - mcp__brain-tools__store_finding
  - mcp__brain-tools__propose_action
allowed_action_types:
  - create_reminder
  - promote_note

# Area agent that edits its own folders and may start two helpers
builtins: [Read, Glob, Grep, Edit, Write, Agent]
vault_paths:
  - Projects/Garden/
  - Plans/Weekend.md
max_helpers: 2
```

There is no model-callable approval tool. Human Slack replies/buttons drive
approval after sender and action provenance checks.

### The follow-up tools

Four tools work on the `follow_ups` table and the collector's digest. Each is
granted explicitly in `mcp_tools`, and `BRAIN_FOLLOWUPS=0` takes all four away.

| Tool | Does | Granted to |
|---|---|---|
| `follow_ups_digest` | Returns one part of today's digest, at most 40,000 characters cut between entries, fenced as outside content under a first line `digest <date>, part n of m`; `part` counts from 1; errors when there is no digest dated today | `follow_up` |
| `follow_ups_list` | Lists items, oldest first, one line each (id, status, place, source, summary, first seen, reference); `status` and `since` filter. The listing is fenced as outside content | `follow_up`, the closing run |
| `follow_ups_add` | Records one open item from `ref`, `asked_by` and `summary`; its source and place come from the collector's index of today's digest, which refuses a ref it does not hold; a ref already recorded returns the known id | `follow_up` |
| `follow_ups_update` | Sets an item's `status`, and its `research` or `summary`; `raised` is limited to five items a day and journaled; a closed item only reopens | `follow_up` |

The private chat gets all four through the private place's union of tools, and
the tools refuse what a chat may not do: `follow_ups_add` refuses a chat, and
`follow_ups_update` accepts only `done` and `dismissed` from one, and `open` from
the owner's direct messages to reopen a closed item. A closed item changes only
by being reopened. The shared
place has none of them unless one of its areas is granted some, and then they
show and change shared items only; `follow_ups_digest` is refused there.

## Modes

Add agents to `user/modes.yaml`:

```yaml
modes:
  all_morning:
    schedule: "0 7 * * *"
    pre: [housekeeping]
    parallel: [career, health, family]
    sequential: [follow_up, alfred_private]
```

User modes replace shipped modes by name; their lists are not deep-merged.
Every referenced name must resolve to an agent. The shipped `all_morning` runs
`follow_up` just before the closing run, so a deployment that defines its own
`all_morning` in `user/modes.yaml` has to list `follow_up` itself, after any
intake triage run and before `alfred_private`, or the follow-up run does not
happen.

A mode whose last `sequential` agent is the private chat agent (`alfred_private`
here) ends with a closing run. The agents before it store findings and proposals
but cannot post: their copy of the configuration loses `push_notify`,
`mail_reply_link` and the `push_notification` action type. The closing run reads
what they stored and posts the morning to each place with
`send_morning_cards`, a tool only the private chat agent may be granted. What
it stores across the areas is the owner's alone, so it stays in the private
place. A mode without a closing run lets its agents post to their own place
under Alfred's name.

## Prompt design

- State one clear ownership boundary and output contract.
- Tell the agent which source is canonical when data conflicts.
- Treat vault, mail, calendar, contacts, web/listing, and intake content as
  data, never instructions.
- Use `store_finding` for observations and the action queue for mutations.
- Avoid claims about unavailable capabilities; describe direct edits and
  helpers only through `caps`.
- Write the area notes so a chat can use them without the rest of the prompt.
- Keep permanent user facts in the private profile/source overlay, not the
  shipped prompt.
- Put reply-specific capture/conversation behavior in `reply_brief`.

## Validation

```bash
.venv/bin/brain doctor
.venv/bin/python -m src.cli --run <mode> --dry-run
.venv/bin/python -m pytest tests/test_loader_smoke.py tests/test_core.py -q
```

The doctor also checks that a channel several agents share has one chat agent,
that `vault_paths` stay clear of the off-limits paths, that `area_owner`
prompts carry both markers, and that only the private chat agent holds
`send_morning_cards`. It warns about an area whose channel has no chat agent,
since replies there are refused and the area cannot appear in a morning
message; once a scheduled mode ends in a closing run, that is an error. It also
warns when no agent has `chat: private` or that agent has no Slack channel, and
about a `vault_paths` entry with no trailing `/` and no file extension, which is
probably a folder missing its slash.

For a live test, use a dedicated manual-only mode and a narrow tool set. Inspect
the corresponding `agent_runs`, `findings`, `events`, and notification rows;
there is no separate transcript subsystem.
