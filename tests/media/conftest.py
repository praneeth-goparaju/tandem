from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.brain.media import svg

CHECKOUT = Path(__file__).resolve().parents[2]
CHILD = (
    sys.executable, "-I", "-c",
    f"import sys; sys.path.insert(0, {str(CHECKOUT)!r}); "
    "from src.brain.media.svg_render import main; raise SystemExit(main())",
)


@pytest.fixture(autouse=True)
def _render_svg_with_this_checkout(monkeypatch):
    monkeypatch.setattr(svg, "CHILD_COMMAND", CHILD)
