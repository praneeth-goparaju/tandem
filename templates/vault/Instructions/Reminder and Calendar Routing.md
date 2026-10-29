# Reminder and Calendar Routing

## The rule

- Each person or household has one reminders list and one calendar. Everything for that person goes to their list and their calendar; nothing is split across several.
- The same table decides both reminders and calendar events.
- **Always attach an alarm.** A due date on its own never notifies anyone.
- Reminders are authoritative for what is done. The vault is authoritative for what it is.
- When it is unclear who an item is for, use the default list and say so.

## The table

The table is kept once, in `user/profile.yaml` under `reminders.routing`. That file is in the framework folder, not in the vault, and the table is given to every chat through the connector's instructions. Each row names a person, household or topic (`who`), its reminders `list` and, where it applies, its `calendar`. If `user/connector_instructions.yaml` has its own `routing_rows`, those are used instead. Change the table in `user/profile.yaml`, not in this note, so every agent and tool reads the same one.
