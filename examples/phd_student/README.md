# PhD Student example

A minimal demo persona for a PhD student in their thesis years. Shows how
one of the starter agents (e.g. `discovery`, `housing`) would read
`{{ user.focus_areas }}` and behave for this use case without any prompt
changes needed.

## Try it

```bash
make demo
```

The command points `USER_DIR` at this tracked example, uses its noop integration
overlay, and dry-runs the scout mode. It does not overwrite your private
`user/` files or `config/integrations.yaml`.

The starter agents (`discovery`, `housekeeping`, `alfred`, plus domain ones
like `housing`) all read `user/profile.yaml` and substitute
`{{ user.name }}`, `{{ user.focus_areas }}`, etc. into their prompts — so the
PhD-student context flows through without any agent-file edits.

For domain-specific agents (e.g. a `paper_submission_deadline_watcher`),
drop a new markdown file into `user/agents/` — see CONTRIBUTING.md for the
frontmatter schema.
