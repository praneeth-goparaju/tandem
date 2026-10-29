# Execution System

Every open obligation (a task, a promise, a reply you are waiting for) is one note in this folder. [[Actions.base]] lists them in three views: Next, Waiting and Done.

## File name

`A-YYYYMMDD-NN - Outcome.md`

- `A-YYYYMMDD-NN` is the item's id: the date it was added and a two-digit number, `01` for the first item that day, `02` for the next.
- `Outcome` is the result that closes the item, written as a short phrase.

## Note format

```
---
type: action
action_id: A-YYYYMMDD-NN
status: next
priority: normal
due: YYYY-MM-DD
waiting_on: "who it waits on"
follow_up: YYYY-MM-DD
added: YYYY-MM-DD
updated: YYYY-MM-DD
source: "where it came from"
tags: [action]
---

# Outcome

Source: where it came from
```

`due`, `waiting_on`, `follow_up` and `source` are left out when they do not apply. A `waiting` item always has both `waiting_on` and `follow_up`.

## Status

- `next`: the next step is the owner's.
- `waiting`: someone else has the next step; `waiting_on` says who and `follow_up` says when to check.
- `done`: finished, or no longer needed. Add a line under the heading saying which.

Change `status` and `updated` when an item moves. Never delete an item note; set it to `done` instead.
