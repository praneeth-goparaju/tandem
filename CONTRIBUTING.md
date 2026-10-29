# Contributing

## Setup

```bash
git clone <repo>
cd brain
make install
install -d -m 700 user
install -m 600 examples/phd_student/profile.yaml user/profile.yaml
install -m 600 examples/phd_student/sources.yaml user/sources.yaml
make test
```

Python 3.12 and macOS are required. Live agent runs also need a Claude
subscription signed in with Claude Code; tests never start Claude.

## The `user/` overlay

Everything that belongs to one person lives in `user/` (or the folder `USER_DIR`
names): `profile.yaml`, `vault_connector.yaml`, `connector_instructions.yaml`,
`sources.yaml`, private agents under `user/agents/`, `user/modes.yaml`, the
rendered launchd jobs and the private term list `user/private_terms.txt`. The
folder is gitignored and is never committed. Shipped files in `config/` hold
defaults and rules only; a file in `user/` with the same name or agent name wins.
Examples to copy from are in `examples/`.

## No personal values in the tracked tree

No real name, nickname, domain, user name, employer, client, product, place or
owner-only vault path goes into a tracked file, not even as test data. Use
neutral stand-ins: "the owner", "the partner", "Child A", "List A", "Topic",
`Folder/Note.md`, `<name>.example.com`, `$HOME`, `/Users/me`, and addresses at
`example.com` or another reserved domain.

```bash
make scan    # tools/check_pii.py --tree --private
```

`make scan` checks every tracked file for home paths, e-mail addresses and the
terms in your private `user/private_terms.txt` (one term per line, full names
rather than short words) and in the gitignored `tools/pii_blocklist.txt` that
`make hooks-pii` generates from your profile, when they exist. When neither
file exists it prints one line saying that only the public rules were used. CI
runs the same scan without the private terms (`python3 tools/check_pii.py
--tree`), and a test fails when the tree does not pass it.

## Writing style

Write plain English. Do not use em dashes in new text: docs, templates, prompts
and the connector's instructions (the connector refuses to start when its
rendered instructions contain one). Say what the code does in ordinary words
instead of inventing terms.

## Layout

```text
config/                 shipped agents, modes, integration defaults
user/                   private overrides; never committed
src/brain/runtime/      config validation and prompt rendering
src/brain/mcp/          brain-tool registry and Python bridge
src/brain/actions/      action policy, queue, executor, handlers
src/brain/plugins/      notifier/listener and task backends
src/orchestrator/       scheduler, executor, reply routing
src/orchestrator/runners/  Claude runner, tool-call rules, brain-tool server
src/brain/connector/    the vault connector (MCP over HTTP for the Claude app)
templates/vault/        the starter vault that init-vault copies
deploy/launchd/         launchd job templates for the connector
examples/               example private files to copy into user/
tests/                  Python tests
```

## Add a brain tool

1. Add an async handler under `src/brain/mcp/tools/` with `@brain_tool`.
2. Import its category in `src/brain/mcp/server.py` so registration occurs.
3. Grant `mcp__brain-tools__<name>` only to agents that need it.
4. Add registry, authorization, result-shape, and failure tests.

Handlers return content blocks:

```python
return {"content": [{"type": "text", "text": "..."}]}
```

Pass caller/area provenance into mutations and treat all external content as
untrusted data.

## Add an action

1. Add the action type and input schema.
2. Set its server-side risk floor; never trust a model-supplied downgrade.
3. Implement a handler with typed success/transient/permanent outcomes.
4. Register the handler.
5. Add an exact Slack approval preview for confirm-gated effects.
6. Add state-transition, deduplication, retry, and idempotency tests.

Actions with the same logical payload need a stable database dedupe key. Human
approval must remain bound to the immutable origin/payload/version record.

## Add a plugin backend

Plugins exist only for swappable notification/listener and task surfaces.

1. Implement the relevant protocol in `src/brain/plugins/base.py`.
2. Register the class in `src/brain/plugins/loader.py`.
3. Add protocol and loader tests in `tests/test_plugins_smoke.py`.
4. Document options and secret handling in `docs/INTEGRATIONS.md`.

Do not introduce a plugin protocol for a direct one-off tool or code job.

## Tests and quality gates

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest tests -q
.venv/bin/python -m mypy
make scan
make audit
make hooks-install
```

Use the isolated database fixture for tests that touch SQLite. Use fake clients
for external services; automated tests must not send Slack messages, mutate the
vault, or contact live accounts.

## Style

- Use Pydantic for external/config schemas and typed mappings/dataclasses at
  runtime boundaries.
- Keep blocking network, IMAP, CalDAV, and macOS APIs off the asyncio event
  loop.
- Use atomic writes with permissions and ownership preserved.
- Centralize schema changes and make upgrade paths transactional/tested.
- Preserve unrelated work in a dirty tree.
- Keep completed implementation plans in Git history rather than the active
  repository.

## Pull requests

Require the Python and runtime CI jobs, dependency audits and conversation
resolution before merge. To require code-owner review as well, put your GitHub
handle in `.github/CODEOWNERS` first (its comments say how). Do not allow direct
force pushes or branch deletion on the protected default branch.
