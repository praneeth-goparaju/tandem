# People diary setup

The `humans` agent captures and recalls person notes from the configured vault,
and posts occasion, follow-up, and meeting-prep reminders to its Slack channel.
Suggested occasion wording is never sent to the person automatically.

## Enable the agent

1. Choose the diary's place: the shared channel or the owner's direct
   messages, as described in [INTEGRATIONS.md](INTEGRATIONS.md).
2. Put that channel's ID in the private `user/integrations.yaml` overlay under
   `notifier.options.channels.humans.channel_id`; keep shipped placeholders
   unchanged.
3. Configure the owner and any area-scoped co-owners as described in
   [INTEGRATIONS.md](INTEGRATIONS.md). On the shared channel, a co-owner's
   `areas` list must include `humans`.
4. Add the agent to a private scheduled mode, for example:

   ```yaml
   # user/modes.yaml
   modes:
     all_morning:
       schedule: "0 7 * * *"
       pre: [housekeeping]
       parallel: [discovery, humans]
       sequential: [alfred_private]
   ```

5. Run `.venv/bin/python -m src.cli doctor --strict`.
6. Restart the configured launchd service so it loads the new configuration.

The agent can capture a conversation or lasting fact, recall a person's recent
timeline and open threads, close follow-ups, and maintain occasion metadata.
Person notes live under the configured people folder in the private vault.

## Proactive reminders

Scheduled runs can surface:

- upcoming birthdays, anniversaries, and festivals;
- people who are due for a check-in;
- unresolved follow-ups;
- context before today's meetings; and
- a prompt to record useful context after a recent meeting.

Each useful item gets its own Slack topic and ledger finding. Occasion wording
is a copy/edit aid for the user, not an outbound messaging integration.

## Privacy

Diary notes and occasion configuration remain local except for content sent to
the configured model during processing and the resulting Slack notification.
Do not place personal profile, channel, or contact values in tracked config.
