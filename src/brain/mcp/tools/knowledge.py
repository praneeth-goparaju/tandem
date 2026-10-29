from __future__ import annotations

from src.brain.mcp._common import _bracket, _wrap_external
from src.brain.mcp.tool_registry import brain_tool
from src.db import get_db


@brain_tool(
    name="search_knowledge",
    description=(
        "Search the frozen legacy knowledge archive for historical recall only. "
        "It has had no write path since 2026-07-15 and is not authoritative or "
        "necessarily current. Check the vault's source-of-truth note before using "
        "a result in a decision. Returns matching archived entries."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (matches topic, summary, tags)"},
            "tags": {"type": "string", "description": "Filter by tag (optional)"},
            "limit": {"type": "integer", "description": "Max results (default: 10)"},
        },
        "required": ["query"],
    },
)
async def search_knowledge(args):
    conn = get_db()
    query = args["query"]
    limit = args.get("limit", 10)

    if args.get("tags"):
        rows = conn.execute(
            "SELECT topic,summary,tags,relevance_score,first_discovered,last_updated,recurrence,next_relevant_date FROM knowledge WHERE tags LIKE ? ORDER BY relevance_score DESC LIMIT ?",
            (f"%{args['tags']}%", limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT topic,summary,tags,relevance_score,first_discovered,last_updated,recurrence,next_relevant_date FROM knowledge WHERE topic LIKE ? OR summary LIKE ? OR tags LIKE ? ORDER BY relevance_score DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", f"%{query}%", limit)).fetchall()

    if not rows:
        return {
            "content": [{
                "type": "text",
                "text": f"No knowledge found for {_bracket(query)}.",
            }]
        }

    lines = []
    for r in rows:
        summary = _wrap_external(str(r[1] or ""), source="knowledge/archive-summary")
        line = (
            f"• {_bracket(r[0])} (relevance: {r[3]:.1f})\n"
            f"  Summary:\n{summary}\n"
            f"  Tags: {_bracket(r[2])} | First: {_bracket(r[4])} "
            f"| Updated: {_bracket(r[5])}"
        )
        if r[6]:
            line += f" | Recurs: {_bracket(r[6])}"
        if r[7]:
            line += f" | Next: {_bracket(r[7])}"
        lines.append(line)
    return {"content": [{"type": "text", "text": "\n\n".join(lines)}]}
