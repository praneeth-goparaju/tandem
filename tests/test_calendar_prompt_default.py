from pathlib import Path


def test_shared_agent_prompt_uses_icloud_as_universal_default() -> None:
    prompt = Path("config/agents/_icloud_surface.md").read_text(encoding="utf-8")
    assert "default and canonical writable calendar is *iCloud*" in prompt
    assert "For family events, *Family*" not in prompt
