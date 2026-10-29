from __future__ import annotations

import src.brain.mcp.tools.action_notes  # noqa: F401
import src.brain.mcp.tools.actions  # noqa: F401
import src.brain.mcp.tools.alfred  # noqa: F401
import src.brain.mcp.tools.drafts  # noqa: F401
import src.brain.mcp.tools.findings  # noqa: F401
import src.brain.mcp.tools.follow_ups  # noqa: F401
import src.brain.mcp.tools.garmin  # noqa: F401
import src.brain.mcp.tools.housing  # noqa: F401
import src.brain.mcp.tools.humans  # noqa: F401
import src.brain.mcp.tools.icloud_actions  # noqa: F401
import src.brain.mcp.tools.icloud_read  # noqa: F401
import src.brain.mcp.tools.knowledge  # noqa: F401
import src.brain.mcp.tools.mail_tools  # noqa: F401
import src.brain.mcp.tools.media  # noqa: F401
import src.brain.mcp.tools.memory  # noqa: F401
import src.brain.mcp.tools.morning_cards  # noqa: F401
import src.brain.mcp.tools.notifications  # noqa: F401
import src.brain.mcp.tools.occasions  # noqa: F401
import src.brain.mcp.tools.ops  # noqa: F401
import src.brain.mcp.tools.replies  # noqa: F401
import src.brain.mcp.tools.tasks  # noqa: F401
import src.brain.mcp.tools.web  # noqa: F401
from src.brain.mcp.tool_registry import ToolSpec, all_tools


def load_brain_tools() -> tuple[ToolSpec, ...]:
    return tuple(all_tools())
