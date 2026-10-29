<!--
Thanks for contributing! A few things that make review fast:

1. Give the PR a clear one-line title — what changes, not how.
2. Link any related issue: "Closes #123".
3. Fill in the sections below. Delete ones that don't apply.
-->

## What

<!-- 1-3 sentences. What behavior changes? -->

## Why

<!-- The problem this solves. If it's a feature, what motivated it. -->

## How

<!-- Brief outline of the approach. Skip if the change is mechanical. -->

## Checklist

- [ ] `pytest -q` passes locally
- [ ] `ruff check .` reports no issues
- [ ] `mypy` passes
- [ ] `python -m src.cli doctor --strict` passes for the intended deployment overlay
- [ ] Security-sensitive dependency or workflow changes are pinned and audited
- [ ] For a new agent: added to `config/agents/*.md` with valid frontmatter (see `docs/AGENT_AUTHORING.md`)
- [ ] For a new plugin: added a conformance test to `tests/test_plugins_smoke.py`
- [ ] For a new brain tool: decorated with `@brain_tool`, imported by `src/brain/mcp/server.py`, and explicitly granted
- [ ] For a durable side effect: routed through the action queue with retry/idempotency tests
- [ ] For database/backup changes: migration and restore-drill documentation updated
- [ ] Docs updated (`README.md`, `docs/*`, or agent markdown as appropriate)
- [ ] `CHANGELOG.md` entry added under `[Unreleased]` (user-visible changes only)

## Test plan

<!--
How did you verify this works? Example:
- Ran `make demo` — tracked all-noop example dry-run completes
- Ran `python -m src.cli --run scout --dry-run` — discovery dry-run succeeds
- Manually reacted to a test push with ✅ — finding marked done in DB
-->
