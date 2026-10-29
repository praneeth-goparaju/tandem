from __future__ import annotations

import struct
import subprocess
import sys
import textwrap
import tomllib
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from src.brain.media import svg_render
from src.brain.media.svg import SvgError

CHECKOUT = Path(__file__).resolve().parents[2]
NS = 'xmlns="http://www.w3.org/2000/svg"'
OK = f'<svg {NS} width="200" height="100"><rect width="200" height="100" fill="teal"/></svg>'


@pytest.fixture(autouse=True)
def _never_limit_the_test_process(monkeypatch):
    monkeypatch.setattr(svg_render, "limit_memory", lambda: None)


def _main(text):
    stdout, stderr = BytesIO(), StringIO()
    code = svg_render.main(BytesIO(text.encode()), stdout, stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def _program(body):
    return [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(CHECKOUT)!r})\n" + textwrap.dedent(body)]


def test_main_writes_the_png_to_stdout_and_exits_zero():
    code, png, stderr = _main(OK)
    assert code == 0 and stderr == ""
    assert struct.unpack(">II", png[16:24]) == (200, 100)


def test_main_refuses_a_document_without_a_size():
    code, png, stderr = _main(f'<svg {NS}><rect width="1" height="1"/></svg>')
    assert (code, png) == (svg_render.EXIT_REFUSED, b"") and "viewBox" in stderr


def test_main_exits_with_the_memory_code_when_the_render_runs_out_of_memory(monkeypatch):
    def too_big(text):
        raise MemoryError

    monkeypatch.setattr(svg_render, "render", too_big)
    code, png, _ = _main(OK)
    assert (code, png) == (svg_render.EXIT_MEMORY, b"")


def test_the_fetcher_serves_data_urls_and_refuses_everything_else():
    assert svg_render._fetch("data:text/plain;base64,aGk=", "text/plain") == b"hi"
    for url in ("http://example.test/a.png", "file:///etc/hosts", "ftp://example.test/a", ""):
        with pytest.raises(SvgError, match="outside itself"):
            svg_render._fetch(url, "image/png")


def test_the_memory_limit_is_a_gibibyte():
    assert svg_render.MEMORY_LIMIT == 1 << 30


def test_the_limit_is_set_before_cairosvg_is_imported():
    result = subprocess.run(
        _program(f"""
            import io, json
            from src.brain.media import svg_render
            seen = []
            svg_render.limit_memory = lambda: seen.append("cairosvg" in sys.modules)
            out = io.BytesIO()
            code = svg_render.main(io.BytesIO({OK!r}.encode()), out, io.StringIO())
            print(json.dumps([seen, code, out.getvalue()[:8].hex()]))
        """),
        capture_output=True, text=True, timeout=60,
    )
    assert result.stdout.strip() == '[[false], 0, "89504e470d0a1a0a"]', result.stderr


def test_a_child_that_outgrows_the_limit_is_stopped():
    result = subprocess.run(
        _program("""
            import time
            from src.brain.media import svg_render
            svg_render.MEMORY_LIMIT = 300 << 20
            svg_render.limit_memory()
            try:
                blob = bytearray()
                for _ in range(60):
                    blob += bytes(16 << 20)
                    time.sleep(0.01)
            except MemoryError:
                raise SystemExit(svg_render.EXIT_MEMORY)
            print("not stopped")
        """),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == svg_render.EXIT_MEMORY and "not stopped" not in result.stdout, result.stderr


def test_the_project_needs_cairosvg_2_9_or_later():
    dependencies = tomllib.loads((CHECKOUT / "pyproject.toml").read_text())["project"]["dependencies"]
    [cairosvg] = [r for r in map(Requirement, dependencies) if r.name.lower() == "cairosvg"]
    assert cairosvg.specifier.contains("2.9.0") and not cairosvg.specifier.contains("2.8.99")
