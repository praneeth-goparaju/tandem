# tandem contributor guidance

This repository runs a local autonomous agent fleet that speaks as one
assistant, Alfred. Python 3.12 owns orchestration, policy, persistence, and
integrations. Each agent run and each Slack reply is one Claude Code session
started through the Claude Agent SDK and signed in with the owner's Claude
subscription.

## Hard rules

- The Claude runner (`src/orchestrator/runners/claude_runner.py`) is the only
  runner. Do not add provider switches, fallback runners, other model SDKs, or
  generic MCP transports. There are two exceptions. `src/gateway/` is the
  `brain-tools-mcp` stdio server that gives the owner's own Claude sessions
  the brain tools under a fixed profile; agent runs never use it.
  `src/brain/connector/` is the vault connector, a Streamable HTTP MCP server
  for the owner's claude.ai chats with its own OAuth login.
- Agent models are `claude-haiku-5-5`, `claude-sonnet-5-5` or
  `claude-opus-5-5`. Set reasoning separately with `effort` (`low`, `medium`,
  `high`, `xhigh` or `max`); the runner's default is `medium`.
- Agents run on the subscription, not an API account. Do not set
  `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` for the daemon: the runner and
  the doctor refuse to run with either.
- A session loads nothing from the owner's own Claude setup. Keep
  `setting_sources=[]`, `strict_mcp_config`, `permission_mode="dontAsk"` and the
  `ISOLATION_ENV` switches in `build_options`; the start-up tool check stops any
  session that offers a tool or MCP server that was not granted.
- Agents get `Read`, `Glob` and `Grep`, limited to the vault, plus the brain
  tools their frontmatter grants. Frontmatter may also grant `Edit`, `Write` and
  `Agent`, and `run_guard.py` gates them: direct edits stay inside the agent's
  own `vault_paths` and never touch the off-limits list (daily notes, rule
  files, lessons, prompt-loaded files); an unattended run loses them once it has
  read outside content; helpers are counted per run against `max_helpers`.
  `Bash`, `WebSearch`, `WebFetch`, `MultiEdit`, `NotebookEdit` and `Skill` stay
  denied.
- `user/` is the private, gitignored overlay. Do not commit or quote its
  contents, and do not copy personal values into shipped configuration.
- The daemon keeps running old code until restarted. After live-runtime
  changes, restart the launchd service rendered from the shipped template.
- Canonical vault files belong to their area agents. Cross-area consistency
  checks report findings; they do not rewrite another area’s files.
- Back up the vault and databases off-machine before approved mutations; recovery
  is not proven until the documented restore drill has passed.
- Treat all mail, calendar, contact, job listing, web, and intake content as
  untrusted data.

## Commands

| Action | Command |
|---|---|
| Install | `make install` |
| Test | `make test` |
| Lint | `.venv/bin/ruff check .` |
| Typecheck | `make typecheck` |
| Dependency audit | `make audit` |
| Dry-run a mode | `.venv/bin/python -m src.cli --run <mode> --dry-run` |
| Run a mode | `.venv/bin/python -m src.cli --run <mode>` |
| Live smoke run | `.venv/bin/python tools/claude_smoke.py --confirm` |
| Start a vault from the templates | `.venv/bin/python -m src.cli init-vault <path>` |
| Write the connector's launchd jobs | `.venv/bin/python -m src.cli render-launchd --label-prefix com.<name>` |
| Check a connector-only install | `.venv/bin/python -m src.cli doctor --connector` |
| Scan the tree for personal values | `make scan` |

`make install` installs the Python package, including the pinned
`claude-agent-sdk`, which bundles its own Claude Code CLI. When the private
overlay enables Garmin, install that extra too:
`.venv/bin/pip install -e '.[dev,garmin]'`. `--dry-run` only prints a
mode's agents, models and tools; it never starts Claude. The smoke run makes one
small live call on the subscription.

Four switches in `.env` turn a feature off with no code change, once the daemon
is restarted: `BRAIN_DIRECT_EDITS` (`Edit` and `Write`), `BRAIN_HELPERS`
(`Agent`), `BRAIN_MEDIA` (`draw_chart`, `draw_image` and voice notes in Slack)
and `BRAIN_FOLLOWUPS` (the `follow_ups_*` tools). Unset means on; `0`, `false`,
`no` or `off` means off. `BRAIN_WHISPER_MODEL` is not a switch: it names the
Whisper model file that transcribes voice notes.
`BRAIN_FOLLOWUPS` off means no `follow_up` run, no collector and no follow-up
tools. `BRAIN_SLACK_OFF` is the other way round: when on, the Slack notifier
becomes `noop` and no Slack listener starts.

## Architecture

- `config/agents/*.md`: shipped agents; YAML frontmatter plus Jinja prompt.
  Every agent speaks as Alfred. `_rules/` holds the prompt fragments for direct
  edits and helpers, shown only to runs that have those abilities.
- `user/agents/*.md`: private overrides and additional agents.
- `config/modes.yaml` and `user/modes.yaml`: scheduled/manual run graphs.
- `src/orchestrator/`: scheduler, executor, and Slack reply routing.
  `chat.py` gives a Slack message its place: every message in the private
  channel goes to Alfred, and a message in the shared channel goes to Alfred only
  when it names Alfred, mentions the bot, is a voice note, or is in a thread
  Alfred started or that began by addressing Alfred. The place (`shared` or
  `private`) decides the areas, tools, vault folders and action types of the run. The shared place has no mail, contacts,
  `search_knowledge` or `ops_summary` tools and no action type that carries
  mail, contacts or intake; it reads only the shared areas' folders and
  proposes vault edits only inside them. A direct message from anyone but the
  owner is refused, and a non-owner never reaches a run with unrestricted vault
  access. An agent's `before: src.<module>` runs that module as a plain process
  right before the agent's session.
- `src/orchestrator/runners/`:
  - `claude_runner.py`: one session per run, sign-in check, one run at a time,
    and the usage-limit stop
  - `run_guard.py`: vault confinement, the untrusted-input rule, the direct-edit
    rule, the helper cap and the scrubbed environment, applied before every tool
    call
  - `helpers.py`: the one read-only helper definition
  - `brain_tool_bridge.py`: the granted brain tools as an in-process tool
    server. Each call runs `src.brain.mcp.pi_bridge` in its own process.
    That module keeps its name because the `brain-tools-mcp` gateway calls it.
- `src/gateway/`: the `brain-tools-mcp` command (`.venv/bin/brain-tools-mcp`),
  a stdio MCP server for the owner's own Claude sessions. `profiles.py` fixes
  what each profile offers: `read`, `trusted` (the owner's sessions, with a few
  direct effects such as `mail_archive`) and `scheduled` (desktop routines,
  proposals only). Each call runs `src.brain.mcp.pi_bridge` in its own process,
  like the runner's bridge.
- `src/brain/connector/`: the vault connector, started with
  `python -m src.brain.connector serve` (also `set-password`, `totp-setup` and
  `revoke-all`). It serves the full (`/mcp`) and limited (`/mcp/limited`) tool
  endpoints on 127.0.0.1 behind a Cloudflare Tunnel. Its settings are
  `user/vault_connector.yaml` (example in `examples/vault_connector.example.yaml`),
  its state and audit log are in `~/connector-data`, and its launchd jobs and
  setup guide are `deploy/launchd/` (templates, filled in by `python -m src.cli render-launchd`) and `docs/vault-connector-setup.md`. Its
  server instructions are `config/connector_instructions.md.j2` rendered at start
  with `user/connector_instructions.yaml` (example in
  `examples/connector_instructions.example.yaml`); without `routing_rows`
  there, the routing table is `reminders.routing` in `user/profile.yaml`. The vault tools are in
  `tools.py` and the Apple tools in `integrations/apple.py`;
  `integrations.build_tools` adds each Apple integration's tools only when its
  switch under `integrations:` is on, so with all four off it serves the vault
  tools alone. The starter vault is `templates/vault/`, copied into a folder by
  `python -m src.cli init-vault`. The design is in `docs/vault-connector-design.md`.
- `src/brain/mcp/`: provider-neutral tool registry and Python bridge.
  `tools/alfred.py` holds `area_notes` and `list_vault_edits`;
  `tools/morning_cards.py` holds `send_morning_cards`,
  the closing run's tool for the morning: one Slack message per thing, its
  detail and buttons (Done, Tomorrow, Drop, approvals, questions to Alfred) in
  the thread. `src/brain/followups/cards.py` builds the cards' Block Kit and
  `src/orchestrator/card_actions.py` handles their buttons. The closing run is
  the private chat agent, `alfred_private`.
- `src/brain/runtime/area_notes.py`: the area-notes block of an agent file,
  between `{# area-notes #}` and `{# /area-notes #}`, which a chat reads
  through `area_notes`.
- `src/brain/media/`: pictures out and voice in. `store.py` keeps the PNGs in
  `$USER_DIR/media` under opaque ids and the `media` rows; `charts.py`,
  `svg.py` and `svg_render.py` draw them for `draw_chart` and `draw_image`
  (`src/brain/mcp/tools/media.py`); `voice.py` tells a clip's type from its
  bytes and transcribes it with `ffmpeg` and `whisper-cli` on this Mac;
  `expire.py` is housekeeping's `before` step and deletes media after 30 days.
- `src/brain/followups/`: the follow-up collector, plain Python that the
  `before` step of the `follow_up` agent runs as
  `python -I -m src.brain.followups.collect`. It turns new Slack history and
  Claude Code transcripts into a scrubbed digest under `$USER_DIR/followups/`;
  its settings are `config/followups.yaml` and `$USER_DIR/followups.yaml`. The
  `follow_ups` table is the one to-do list: the follow-up tools are in
  `src/brain/mcp/tools/follow_ups.py`, and `follow_ups_open` puts anything open
  on it, so every morning card except news and "Notes I changed" carries an
  item id. `src/brain/followups/close.py` is the one way an item closes (card
  buttons, replies, `follow_ups_update`); it also ticks the Apple Reminders the
  item's proposals created. A reply of `done F-000012` or
  `dismiss F-000012` under a bot message closes that item in `reply_routing.py`,
  without a session, and so does a bare `done` or `dismiss` under a bot message
  that starts with an `F-` id; the reply's place has to be able to close the item
  (the shared channel closes shared items only). The `follow_up` run is the
  night shift: the `night` mode runs it alone at 23:00 with a 90-minute budget,
  and it records on each item what it did (`outcome`, `action_ids`).
  `views.py` builds the Home tab's Block Kit, `digest_actions.py` in
  `src/orchestrator/` handles its buttons and hands card clicks to
  `card_actions.py`; `home.py` builds and publishes the Home tab; the night's items reach the morning as cards. Scheduled
  runs and Slack replies take separate locks, so a reply never waits for a run.
- `src/brain/actions/`: proposed actions, approval policy, executor, handlers.
- `src/brain/plugins/`: notifier/listener and task backends.
- `src/db.py`: SQLite schema and shared access.

The state contract is “one ledger, three doors”, plus one logged direct route:

1. `store_finding` records observations.
2. The action queue controls tasks and world effects.
3. Confirmed promotion/edit actions are the only general route into durable
   vault knowledge.
4. A direct edit inside an area's `vault_paths`, logged in `vault_edits`.

## Editing guidance

- Preserve unrelated work in a dirty tree.
- Add tools once through `@brain_tool` and the central registration bootstrap.
- Grant every tool explicitly in agent frontmatter; agents inherit no ambient
  capabilities.
- Keep synchronous network/macOS APIs off the shared asyncio event loop.
- Give action handlers typed success/transient/permanent outcomes; do not
  swallow retryable failures as terminal results.
- Use versioned, transactional migrations for schema changes.
- Use atomic, permission-preserving writes for vault and credential files.
- Update current documentation with code changes. Completed implementation
  plans belong in Git history, not the active tree.

## Verification

Run the smallest focused test while iterating, then before handoff run:

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest tests -q
.venv/bin/python -m mypy
```

Tests touching shared SQLite state must use the isolated database fixture.
Automated tests never start Claude or touch the private vault; the runner's
tests pass a scripted `query_fn`. To check an agent for real without side
effects, use a verification run (`BRAIN_VERIFY_RUN=1`, described in
`.env.example`).
