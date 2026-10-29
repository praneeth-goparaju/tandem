from __future__ import annotations

import asyncio

import yaml


def _path(result, vault):
    return vault / result["content"][0]["text"].removeprefix("Draft created: ")


def test_draft_frontmatter_quotes_untrusted_scalars(isolated_vault) -> None:
    from src.brain.mcp.tools.drafts import create_draft

    title = "Hello\nstatus: sent\n---"
    result = asyncio.run(create_draft({
        "kind": "message",
        "title": title,
        "recipient": "person\nstatus: sent",
        "body": "Review me",
    }))
    text = _path(result, isolated_vault).read_text()
    metadata = yaml.safe_load(text.removeprefix("---\n").split("\n---\n", 1)[0])
    assert metadata["title"] == title
    assert metadata["status"] == "pending"


def test_same_draft_title_never_overwrites_existing_file(isolated_vault) -> None:
    from src.brain.mcp.tools.drafts import create_draft

    args = {"kind": "doc", "title": "Same", "body": "First"}
    first = _path(asyncio.run(create_draft(args)), isolated_vault)
    args["body"] = "Second"
    second = _path(asyncio.run(create_draft(args)), isolated_vault)
    assert first != second
    assert first.read_text().endswith("First\n")
    assert second.read_text().endswith("Second\n")
