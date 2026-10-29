from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

DELIVERY_SUCCESS_STATES = frozenset({"sent", "noop"})


def media_outcome_note(result: Mapping[str, Any]) -> str:
    lines = []
    refused = result.get("refused_media") or []
    if refused:
        lines.append(
            "refused media: " + ", ".join(refused)
            + " (no such media, deleted, or made for another place)"
        )
    failed = result.get("failed_media") or []
    if failed:
        errors = result.get("media_errors") or {}
        described = [
            f"{media_id} ({errors[media_id]})" if errors.get(media_id) else media_id
            for media_id in failed
        ]
        lines.append("failed media: " + ", ".join(described))
    return "\n".join(lines)


class TaskCreateOutcomeUnknown(RuntimeError):
    """A task-create request may have committed, so it must not be replayed.

    Backends without a durable provider-side idempotency key raise this after
    create dispatch when they cannot prove whether the task was persisted.
    """


@runtime_checkable
class Notifier(Protocol):
    """Push a notification to the user.

    `urgency` is advisory — Slack may map it to message presentation and noop
    ignores it. Known values: "info", "important", "urgent".
    """

    async def send(
        self,
        text: str,
        *,
        title: str = "",
        urgency: str = "info",
        finding_id: str | None = None,
        event_type: str | None = None,
        area: str | None = None,
        agent: str | None = None,
        quote_timestamp: int | str | None = None,
        idempotency_key: str | None = None,
        plain_text: bool = False,
        proposal_action_ids: list[str] | None = None,
        require_explicit_area: bool = False,
        media_ids: list[str] | None = None,
        place: str | None = None,
    ) -> dict[str, Any]:
        """Return a dict with at least {'status': 'sent'|'failed'|'noop', 'id': ...}.

        `event_type` marks a message that carries no finding for whoever reads the
        channel's history later; Slack sends it as message metadata.

        The returned id (if any) lets the caller correlate user reactions/replies
        back to the original notification. For Slack that is the message `ts`
        string; noop returns None. `plain_text` disables transport markup for
        verbatim, externally sourced text. `proposal_action_ids` is the only
        supported route for attaching scoped human-approval controls; notifier
        implementations must not infer proposals from message prose.
        `require_explicit_area` forbids a transport's default destination.
        `media_ids` (at most 4) name stored pictures to post after the message,
        in its thread; one that does not exist, was deleted, or was made for
        another place than `place` (default: the place of `area`) is left out
        and reported in the result's `refused_media`, an upload that fails in
        `failed_media` and `media_errors`. More than 4 fail the whole send.
        """
        ...


@runtime_checkable
class TaskManager(Protocol):
    async def add(
        self,
        title: str,
        *,
        description: str = "",
        project: str | None = None,
        priority: int | None = None,
        due: str | None = None,
        labels: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a task. Returns {'id': str, 'url': str | None}.

        Implementations that honor ``idempotency_key`` may safely propagate a
        transient failure for retry. Implementations without that guarantee
        must raise :class:`TaskCreateOutcomeUnknown` after uncertain dispatch.
        """
        ...

    async def list_pending(self, *, project: str | None = None) -> list[dict[str, Any]]:
        """Return list of active tasks. Each dict has at least {id, title, due}."""
        ...

    async def list_completed(
        self, *, since_hours: int = 24, limit: int = 50
    ) -> list[dict[str, Any]]:
        ...

@runtime_checkable
class ReplyListener(Protocol):
    """Listens for inbound replies/reactions from the user.

    Each event is passed to `callback` as a normalized dict envelope:

        {
          "type": "reply" | "reaction",
          "text": str,              # reply body (reply only)
          "emoji": str | None,      # reaction name without colons (reaction only)
          "sender": str,            # transport-specific user id
          "group_id": str,          # transport-specific channel/group id (Slack channel id)
          "timestamp": str,         # transport message id (Slack ts)
          "quoted_timestamp": str | None,  # thread root / quoted message id
          "target_timestamp": str | None,  # reaction target id (reaction only)
          "quoted_text": str,       # text of the quoted/root message if available
          "quoted_from_bot": bool,  # that message was posted by a bot (reply only)
          "finding_id": str | None, # parsed from `ref:<id>` if present
          "images": list[dict],      # model-ready images, empty until authorized
          "image_count": int,        # attached images observed by the listener
          "_load_images": callable,  # private one-shot loader; authorize before calling
          "_load_voice": callable | None,  # private one-shot clip loader; authorize before calling
          "voice_attached": bool,    # the clip is an audio file the sender attached, not a Slack clip
        }

    The `group_id` field name is a back-compat label for the area lookup
    helper; on Slack it holds the channel id.
    """

    async def run(self, callback) -> None:
        ...
