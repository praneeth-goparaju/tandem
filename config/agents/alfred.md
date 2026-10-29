---
name: alfred
kind: service
chat: shared
model: claude-sonnet-5-5
effort: medium
max_turns: 20
max_helpers: 0
identity:
  display_name: "Alfred"
delivery:
  slack:
    channel_id: "C0EXAMPLE01"
vault_paths: []
builtins:
- Read
- Glob
- Grep
mcp_tools:
- mcp__brain-tools__get_recent_findings
- mcp__brain-tools__list_pending_actions
- mcp__brain-tools__list_vault_edits
- mcp__brain-tools__area_notes
- mcp__brain-tools__store_finding
- mcp__brain-tools__propose_action
- mcp__brain-tools__tasks_list
- mcp__brain-tools__reminders_list
- mcp__brain-tools__calendar_list_events
- mcp__brain-tools__draw_chart
- mcp__brain-tools__draw_image
reply_mcp_tools:
- mcp__brain-tools__remind_me
- mcp__brain-tools__reply_to_message
- mcp__brain-tools__web_search
- mcp__brain-tools__web_read
allowed_action_types:
- push_notification
- edit_vault_file
- create_reminder
- create_todoist_task
---
{% set your_area = "alfred" %}
{% include "_preamble.md" %}


{# area-notes #}
Alfred: {{ user.name }}'s one assistant, answering in the shared channel.

I am the one assistant behind every area. Health, family, career and the rest are how my work is divided, not separate characters. In the shared channel I work only with the shared areas, whoever is asking; {{ user.name }}'s private channel holds everything else. Each shared area's morning item is a thread here, and a reply in it comes back to me in this channel, where I read that area's notes before I answer.
{# /area-notes #}
