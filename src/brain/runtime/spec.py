from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

Model = Literal[
    "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5",
]
Kind = Literal["area_owner", "service", "system"]


class RuntimeContext(TypedDict, total=False):
    """Per-cycle data the runner threads into every agent run of a cycle.

    The first three keys are surfaced into Jinja as `{{ active_mode }}`,
    `{{ expected_agents }}` and `{{ expected_area_owners }}`. `closing_agent`
    is read by the executor, not by prompts: it names the run that closes the
    cycle, the only one that posts. `total=False` because each field is
    independently optional (e.g. tests / ad-hoc invocations may omit all of
    them).
    """

    active_mode: str
    expected_agents: list[str]
    expected_area_owners: list[str]
    closing_agent: str | None


class IdentitySpec(BaseModel):
    display_name: str
    icon_emoji: str | None = None


class SlackDelivery(BaseModel):
    channel_id: str = Field(..., pattern=r"^[CDG][A-Z0-9]+$")


class DeliverySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slack: SlackDelivery | None = None


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    model: Model = "claude-sonnet-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    max_turns: int = Field(30, ge=1, le=200)
    timeout_seconds: int | None = Field(None, ge=60, le=7200)
    description: str = ""

    builtins: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    reply_mcp_tools: list[str] = Field(default_factory=list)
    allowed_action_types: list[str] = Field(default_factory=list)

    kind: Kind | None = None

    identity: IdentitySpec | None = None

    delivery: DeliverySpec | None = None

    reply_brief: str = ""

    chat: Literal["shared", "private"] | None = None

    vault_paths: list[str] = Field(default_factory=list)

    max_helpers: int = Field(0, ge=0, le=3)

    before: str | None = Field(None, pattern=r"^src\.[a-z0-9_]+(\.[a-z0-9_]+)*$")

    body: str = ""

    @field_validator("builtins")
    @classmethod
    def _validate_builtins(cls, v: list[str]) -> list[str]:
        allowed = {"Read", "Glob", "Grep", "Edit", "Write", "Agent"}
        bad = [t for t in v if t not in allowed]
        if bad:
            raise ValueError(f"unknown builtins: {bad}")
        return v

class ModeSpec(BaseModel):
    name: str
    schedule: str | None = None
    pre: list[str] = Field(default_factory=list)
    parallel: list[str] = Field(default_factory=list)
    sequential: list[str] = Field(default_factory=list)
    misfire_grace_seconds: int | None = Field(None, ge=60, le=86400)

    chain: str | None = None

    def agents(self) -> list[str]:
        return [*self.pre, *self.parallel, *self.sequential]
