# Agent runtime

Brain agents run through one path: Claude Code, started through the Claude
Agent SDK and signed in with the owner's Claude subscription. Provider selection
and fallback are intentionally absent, so scheduled, manual and reply runs cannot
drift between different capability or policy models.

## Models

Agent frontmatter accepts:

| Model | Typical use |
|---|---|
| `claude-haiku-5-5` | Fast routing and high-volume structured work |
| `claude-sonnet-5-5` | General area-owner work and chat; the default, and the helpers' model |
| `claude-opus-5-5` | Research, synthesis, and adjudication |

`effort: low|medium|high|xhigh|max` is set separately from the model. The
runner sends `medium` when an agent sets none, because the three models have
different defaults of their own.

## Components

- `src/orchestrator/runners/claude_runner.py` builds the session options, checks
  sign-in, runs one session per agent or reply, applies the overall deadline,
  stops at the subscription's usage limit, and writes one telemetry row.
- `src/orchestrator/runners/run_guard.py` holds the rules applied before every
  tool call, a helper's included: vault confinement, the untrusted-input rule,
  the verification-run rule, the direct-edit rule, and the helper cap. It also
  builds the scrubbed environment.
- `src/orchestrator/runners/helpers.py` defines the one read-only helper a run
  may start.
- `src/orchestrator/runners/brain_tool_bridge.py` offers the granted brain tools
  as an in-process tool server named `brain-tools` and runs each call in a
  bounded child process.
- `src/brain/mcp/pi_bridge.py` loads the central tool registry, validates the
  caller and allowlist, invokes a handler, and returns normalized JSON. It keeps
  its name because the `brain-tools-mcp` gateway (`src/gateway/`, the
  owner's own Claude sessions' tool server) also calls it.

## Authentication

Agents use the Claude login that Claude Code keeps in the macOS keychain. Sign
in once with `claude` in a terminal. The launchd job reads the same login.

The runner and the doctor refuse to run when `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN` is set, so usage is never billed to an API account by
accident. If a launchd job ever cannot read the keychain login, run
`claude setup-token` and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`
(file mode 600).

The subscription's usage limits assume ordinary, individual use. The runner
therefore runs one session at a time across the whole daemon. When Claude
reports that the limit is reached, the run fails with `usage limit reached`,
later runs are skipped until the reset time, and one Slack notice says so.

## Session isolation

Every session is started with:

- `setting_sources=[]`: no settings files, no `CLAUDE.md`, skills, plugins or
  hooks from `~/.claude` or the repository
- `strict_mcp_config=True`: only the `brain-tools` server, never the owner's
  own MCP servers
- `permission_mode="dontAsk"`: anything not pre-approved is denied
- `CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `CLAUDE_CODE_DISABLE_ORG_MEMORY` and
  `CLAUDE_CODE_DISABLE_ATTACHMENTS`: no memory files, and no `AGENTS.md` or
  `CLAUDE.md` attached from the folders above a file an agent reads
- `--no-session-persistence`: the session's transcript is not saved to disk
- the daemon's secrets set to empty: any variable whose name ends in `_TOKEN`,
  `_PASSWORD`, `_KEY`, `_SECRET`, `_CREDENTIALS` or `_CREDENTIALS_PATH` is
  blanked in the session's environment, except the session's own `CLAUDE...`
  sign-in variables

When the session starts, the runner compares the tools and MCP servers it was
offered with what the agent was granted, and stops the run if anything else
appears. `Agent`, `Edit` and `Write` count even though they sit on the denied
list: a session that offers one without a grant is stopped.

## Capability model

A session can receive these built-ins:

- `Read`, `Glob` and `Grep`, limited to the vault. In the shared Slack place
  they reach only the shared areas' `vault_paths`.
- `Edit` and `Write`, only when the agent's frontmatter grants them and
  `BRAIN_DIRECT_EDITS` is not off. `run_guard.py` holds them to the agent's own
  `vault_paths` and the other direct-edit limits in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- `Agent`, only when the frontmatter grants it, `max_helpers` is above 0 and
  `BRAIN_HELPERS` is not off.

Brain tools come from the agent's effective allowlist and are checked again by
the bridge. A tool whose feature switch is off is left out of the grants and
refused by the hook anyway: `draw_chart` and `draw_image` with `BRAIN_MEDIA` off,
and the `follow_ups_*` tools with `BRAIN_FOLLOWUPS` off (`FEATURE_TOOLS` in
`run_guard.py`). `Bash`, `MultiEdit`, `NotebookEdit`, `WebSearch`, `WebFetch` and
`Skill` are denied for every run. `Edit`, `Write` and `Agent` are denied for a
run that is not granted them, and `Task`, the name the Claude Code CLI uses for
`Agent`, goes with `Agent`: the start-up check and the helper limit treat the
two names as one tool. Public web access goes through the brain tools
`web_search` and `web_read`.

## Helpers

A run granted `Agent` can start read-only helpers for research. There is one
helper definition (`helper_definition` in `helpers.py`), passed to the session
through the SDK `agents` option under the type `helper`:

| Setting | Value |
|---|---|
| Model | `claude-sonnet-5-5` |
| Effort | `medium` |
| Turns | at most 15 |
| Tools | `Read`, `Glob`, `Grep`, and the parent's brain tools that only read |
| Denied | `Agent`, `Task`, `Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `WebSearch`, `WebFetch`, `Skill` |

A helper has no `propose_action`, posting or other tool with an effect, and no
web tools: web lookups stay with the parent, which may make three searches per
run. The `PreToolUse` hook counts helper starts against the run's
`max_helpers` (3 at most; a chat run gets 2 when one of its place's areas grants
`Agent` and `BRAIN_HELPERS` is on), refuses any type other than `helper`, and
journals each start as `helper_start`. The SDK fires the session's hooks for a
helper's own tool calls, marked with an `agent_id`, so the hook holds each call
to the helper's tool list and applies vault confinement, the web rule and the
outside-content state, which a helper shares with its parent.

## Result and cancellation contract

The runner returns response text, model, aggregate token and cache usage, cost,
tool-call count, completed turns, stop reason, whether a Slack reply was
delivered, and an optional error, as an `AgentRunResult`. It persists one
terminal `agent_runs` row with `provider = 'claude'`. The `cost_usd` figure is
the SDK's estimate at API prices; on a subscription nothing is charged per run.

The overall timeout starts once a run holds the lock, so a Slack reply that
waits behind a scheduled agent is not cut short by the wait. Each brain-tool
call has its own timeout and output cap, and a stopped run kills the call's
whole process group.

## Verification

```bash
.venv/bin/python -m pytest tests/test_claude_runner.py tests/test_run_guard.py tests/test_helpers.py tests/test_brain_tool_bridge.py -q
.venv/bin/python -m src.cli doctor --strict
.venv/bin/python tools/claude_smoke.py --confirm
```

The smoke run makes one small Claude Haiku call with only `Read` granted and
prints what the session was offered.
