from __future__ import annotations

from collections import defaultdict
from typing import Any

from src.brain.actions.outcomes import ambiguous_failure, partial_success, permanent_failure
from src.brain.mcp.tools import mail_ops

MAX_BATCH = 50


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    """Archive every approved mail over one IMAP session per account and mailbox.

    Each mail must still match the snapshot taken when it was proposed; one that
    changed is skipped and reported, and an unknown outcome makes the result ambiguous.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_BATCH:
        return permanent_failure(f"a batch needs 1 to {MAX_BATCH} messages")
    groups: dict[tuple[str, str], list[tuple[str, dict[str, str] | None]]] = defaultdict(list)
    for message in messages:
        snapshot = {key: str(message.get(key) or "") for key in mail_ops.MAIL_SNAPSHOT_KEYS}
        key = (str(message.get("provider") or ""), str(message.get("mailbox") or "INBOX"))
        groups[key].append((str(message.get("uid") or ""), snapshot))
    archived, failed, unknown = 0, [], []
    for (provider, mailbox), items in groups.items():
        try:
            results = await mail_ops.archive_messages(provider, mailbox, items)
        except Exception as exc:  # noqa: BLE001
            unknown += [f"uid={uid}: {exc}" for uid, _ in items]
            continue
        for uid, outcome in results:
            if not isinstance(outcome, Exception):
                archived += 1
            elif isinstance(outcome, (mail_ops.MailArchiveAmbiguousError, mail_ops.MailArchivePartialError)):
                unknown.append(f"uid={uid}: {outcome}")
            else:
                failed.append(f"uid={uid}: {outcome}")
    result = f"archived {archived} of {len(messages)}"
    if unknown:
        return ambiguous_failure(f"{result}; outcome unknown for {'; '.join(unknown)}")
    if failed:
        return partial_success(f"{result}; not archived: {'; '.join(failed)}")
    return {"ok": True, "result": result}
