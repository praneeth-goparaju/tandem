from __future__ import annotations

import base64
import gzip
import os
import struct
import sys
import time
from io import BytesIO
from urllib.parse import quote

import pytest
from PIL import Image

from src.brain.media import svg
from src.brain.media.svg import SvgError, check_svg, render_svg

OK = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="teal"/></svg>'
NS = 'xmlns="http://www.w3.org/2000/svg"'
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_COMMAND = svg.CHILD_COMMAND


def _size(png):
    return struct.unpack(">II", png[16:24])


def _tiny_png_bytes():
    buffer = BytesIO()
    Image.new("RGBA", (1, 1), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def _tiny_png_base64():
    return base64.b64encode(_tiny_png_bytes()).decode()


def test_svg_renders_to_png_of_its_size():
    png = render_svg(OK)
    assert struct.unpack(">II", png[16:24]) == (200, 100)


@pytest.mark.parametrize("bad, reason", [
    (OK.replace("<rect", "<script>alert(1)</script><rect"), "<script> element"),
    (OK.replace("<rect", "<foreignObject><div/></foreignObject><rect"), "<foreignObject> element"),
    (OK.replace("<rect", '<image href="http://evil/x.png"/><rect'), "outside itself"),
    (OK.replace("<rect", '<a xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="http://evil"/><rect'),
     "outside itself"),
    (OK.replace("<rect", '<style>@import url("http://evil/f.css");</style><rect'), "<style> element refers to a URL"),
    (OK.replace("<rect", '<rect style="fill:url(http://evil)"'), "style attribute refers to a URL"),
    (OK.replace("<rect", '<rect onload="x()"'), "event-handler attribute, onload"),
    ('<!DOCTYPE svg [<!ENTITY e SYSTEM "file:///etc/passwd">]>' + OK, "DOCTYPE"),
    (OK.replace('width="200" height="100"', 'width="5000" height="100"'), "larger than 4096"),
])
def test_svg_refuses_dangerous_documents(bad, reason):
    with pytest.raises(SvgError, match=reason):
        check_svg(bad)


def _data_url(media_type, data, *, base64_encoded=True):
    if base64_encoded:
        return f"data:{media_type};base64,{base64.b64encode(data).decode()}"
    return f"data:{media_type},{quote(data.decode())}"


NESTED = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" onload="x()">'
    b"<script>alert(1)</script><foreignObject><div/></foreignObject></svg>"
)


@pytest.mark.parametrize("href", [
    _data_url("image/svg+xml", NESTED),
    _data_url("image/svg+xml", NESTED, base64_encoded=False),
    _data_url("IMAGE/SVG+XML", NESTED),
    _data_url(" image/svg+xml ;charset=utf-8", NESTED),
    _data_url("image/svg+xml", gzip.compress(NESTED)),
    _data_url("image/svg+xml", _tiny_png_bytes()),
    "data:image/svg+xml;utf8,&lt;svg/&gt;",
])
def test_an_svg_inside_a_data_url_is_refused(href):
    with pytest.raises(SvgError, match="another SVG"):
        check_svg(OK.replace("<rect", f'<image width="10" height="10" href="{href}"/><rect'))


@pytest.mark.parametrize("element", ["image", "use"])
@pytest.mark.parametrize("href", [
    _data_url("image/png", NESTED),
    _data_url("image/png", gzip.compress(NESTED)),
    _data_url("image/png", NESTED, base64_encoded=False),
    _data_url("image/png", b"%!PS-Adobe-3.0 EPSF-3.0\n"),
    _data_url("text/plain", b"hello"),
    _data_url("image/png", b"\x89PNG&#13;&#10;"),
    "data:image/png;base64,",
    "data:image/png;base64,%%%",
    "data:,",
])
def test_a_data_url_that_does_not_hold_a_raster_picture_is_refused(element, href):
    with pytest.raises(SvgError, match="PNG, JPEG, GIF or WebP"):
        check_svg(OK.replace("<rect", f'<{element} width="10" height="10" href="{href}"/><rect'))


def test_a_png_header_split_by_line_breaks_is_read_as_the_renderer_reads_it():
    png = _tiny_png_bytes()
    href = "data:image/png," + quote(png[:4].decode("latin-1"), encoding="latin-1") + "&#13;&#10;" + quote(
        (png[6:8] + NESTED).decode("latin-1"), encoding="latin-1",
    )
    with pytest.raises(SvgError, match="PNG, JPEG, GIF or WebP"):
        check_svg(OK.replace("<rect", f'<image width="10" height="10" href="{href}"/><rect'))


@pytest.mark.parametrize("href", [
    "#r url(data:image/svg+xml;base64,PHN2Zy8+)",
    _data_url("image/png", _tiny_png_bytes()) + " url(data:image/svg+xml;base64,PHN2Zy8+)",
    "#r URL(http://evil/x.svg)",
])
def test_an_href_that_holds_a_url_function_is_refused(href):
    with pytest.raises(SvgError, match="outside itself"):
        check_svg(OK.replace("<rect", f'<use href="{href}"/><rect'))


@pytest.mark.parametrize("media_type, picture", [
    ("image/png", _tiny_png_bytes()),
    ("image/jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
    ("image/gif", b"GIF89a\x01\x00\x01\x00"),
    ("image/gif", b"GIF87a\x01\x00\x01\x00"),
    ("image/webp", b"RIFF\x1a\x00\x00\x00WEBPVP8 "),
])
def test_raster_pictures_in_data_urls_are_allowed(media_type, picture):
    href = _data_url(media_type, picture)
    assert check_svg(OK.replace("<rect", f'<image width="10" height="10" href="{href}"/><rect')) is None


def test_svg_allows_data_uris_and_local_ids():
    image = f'<image width="10" height="10" href="data:image/png;base64,{_tiny_png_base64()}"/>'
    doc = OK.replace("<rect", f'<defs><rect id="r" width="10" height="10"/></defs><use href="#r"/>{image}<rect')
    assert render_svg(doc)[:8] == PNG_MAGIC


def test_the_size_limit_counts_bytes_not_characters():
    doc = OK.replace("<rect", "<!--" + "€" * 200_000 + "--><rect")
    assert len(doc) < 524_288
    with pytest.raises(SvgError, match="512"):
        check_svg(doc)


def test_a_document_just_under_the_limit_is_accepted():
    pad = 524_288 - len(OK.encode()) - len("<!---->")
    assert check_svg(OK.replace("<rect", "<!--" + "x" * pad + "--><rect")) is None


@pytest.mark.parametrize("bad", ["", "not xml", "<svg", f"<svg {NS}><g></svg>", "<svg/>\ud800"])
def test_svg_that_is_not_well_formed_is_refused(bad):
    with pytest.raises(SvgError):
        check_svg(bad)


@pytest.mark.parametrize("prefix", [
    "<!DOCTYPE svg>",
    '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">',
    '<!DOCTYPE svg [<!ENTITY a "b">]>',
    '<?xml version="1.0" encoding="x-bogus"?>',
    '<?xml version="1.0" encoding="UTF-16"?>',
    '<?xml version="1.0" encoding="ascii"?>\n<!-- \u20ac -->',
])
def test_svg_with_a_doctype_or_an_unreadable_encoding_is_refused(prefix):
    with pytest.raises(SvgError):
        check_svg(prefix + OK)


@pytest.mark.parametrize("extra", [
    "<SCRIPT/>",
    '<svg:script xmlns:svg="http://www.w3.org/2000/svg"/>',
    '<use href="http://evil/s.svg#a"/>',
    '<use href=""/>',
    '<use href=" #r"/>',
    '<use href="HTTP://evil/s.svg#a"/>',
    '<image xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="file:///etc/hosts"/>',
    '<a xmlns:foo="http://example.com/ns" foo:href="http://evil"/>',
    "<style>.a{fill:URL(#g)}</style>",
    '<style>@IMPORT "http://evil/f.css";</style>',
    '<rect style="fill:url(#g)"/>',
    '<rect ONCLICK="x()"/>',
    '<rect onmouseover="x()"/>',
])
def test_svg_refuses_more_ways_to_reach_outside_or_run_code(extra):
    with pytest.raises(SvgError):
        check_svg(OK.replace("<rect", extra + "<rect"))


@pytest.mark.parametrize("extra", [
    '<rect style="fill:red"/>',
    "<style>rect{fill:red}</style>",
    '<use xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="#r"/>',
    '<rect fill="url(#g)"/>',
    f'<image width="1" height="1" href="data:image/png;base64,{_tiny_png_base64()}"/>',
])
def test_svg_allows_plain_styling_local_references_and_data_urls(extra):
    assert check_svg(OK.replace("<rect", extra + "<rect")) is None


@pytest.mark.parametrize("attrs", [
    'width="4097" height="10"',
    'width="10" height="4097"',
    'viewBox="0 0 4097 10"',
    'viewBox="0,0,10,4097"',
    'width="100in" height="10"',
    'width="1e4" height="10"',
    'width="9999999pt 1" height="10"',
    'width="300em" height="10"',
    'width="5000px" height="10"',
    'width="inf" height="10"',
    'width="nan" height="10"',
])
def test_svg_roots_larger_than_4096_pixels_are_refused(attrs):
    with pytest.raises(SvgError, match="4096"):
        check_svg(f"<svg {NS} {attrs}/>")


@pytest.mark.parametrize("attrs", [
    'width="4096" height="4096"',
    'width="42in" height="1"',
    'width="255em" height="1"',
    'viewBox="0 0 4096 4096"',
    'width="50%" viewBox="0 0 100 50"',
])
def test_svg_roots_up_to_4096_pixels_are_accepted(attrs):
    assert check_svg(f"<svg {NS} {attrs}/>") is None


def test_a_viewbox_alone_sets_the_size():
    doc = f'<svg {NS} viewBox="0 0 100 50"><rect width="10" height="10"/></svg>'
    assert _size(render_svg(doc)) == (100, 50)


def test_a_document_without_a_size_says_what_it_needs():
    with pytest.raises(SvgError, match="viewBox"):
        render_svg(f'<svg {NS}><rect width="10" height="10"/></svg>')


@pytest.mark.parametrize("href", ["http://127.0.0.1:9/x.png", "file:///etc/hosts"])
def test_a_reference_the_check_missed_is_refused_by_the_renderer(monkeypatch, href):
    monkeypatch.setattr(svg, "check_svg", lambda text: None)
    with pytest.raises(SvgError, match="outside itself"):
        render_svg(OK.replace("<rect", f'<image href="{href}"/><rect'))


def test_an_import_the_check_missed_is_refused_by_the_renderer(monkeypatch):
    monkeypatch.setattr(svg, "check_svg", lambda text: None)
    with pytest.raises(SvgError, match="outside itself"):
        render_svg(OK.replace("<rect", "<style>@import 'http://127.0.0.1:9/f.css';</style><rect"))


def test_a_stylesheet_instruction_is_not_a_way_to_fetch():
    png = render_svg('<?xml-stylesheet href="http://127.0.0.1:9/a.css" type="text/css"?>' + OK)
    assert _size(png) == (200, 100)


def test_the_output_never_exceeds_4096_pixels_even_if_the_check_is_bypassed(monkeypatch):
    monkeypatch.setattr(svg, "check_svg", lambda text: None)
    png = render_svg(f'<svg {NS} width="5000" height="100"><rect width="9" height="9"/></svg>')
    assert _size(png) == (4096, 82)


def test_text_and_gradients_render_in_the_child():
    doc = (
        f'<svg {NS} width="120" height="60"><defs><linearGradient id="g"><stop offset="0" stop-color="red"/>'
        '<stop offset="1" stop-color="blue"/></linearGradient></defs>'
        '<rect width="120" height="60" fill="url(#g)"/><text x="10" y="35" fill="white">Hi</text></svg>'
    )
    assert _size(render_svg(doc)) == (120, 60)


def _python(code, *args):
    return [sys.executable, "-c", code, *args]


PNG_PREFIX = "b'\\x89PNG\\r\\n\\x1a\\n'"


def test_the_default_child_is_this_interpreter_isolated_running_the_render_module():
    assert DEFAULT_COMMAND == (sys.executable, "-I", "-m", "src.brain.media.svg_render")


def test_the_document_goes_to_the_child_on_stdin_and_the_png_comes_back_on_stdout():
    echo = _python(f"import sys; sys.stdout.buffer.write({PNG_PREFIX} + sys.stdin.buffer.read())")
    assert render_svg(OK, command=echo) == PNG_MAGIC + OK.encode()


def test_the_child_does_not_get_the_daemons_environment(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    show = _python(
        f"import os, sys; sys.stdout.buffer.write({PNG_PREFIX} + ','.join(sorted(os.environ)).encode())"
    )
    names = render_svg(OK, command=show)[8:].decode().split(",")
    assert "PATH" in names and "SLACK_BOT_TOKEN" not in names


def test_a_render_that_runs_too_long_is_killed_and_reported(tmp_path):
    pidfile = tmp_path / "pid"
    hang = _python("import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)", str(pidfile))
    started = time.monotonic()
    with pytest.raises(SvgError, match="render timed out"):
        render_svg(OK, timeout=2.0, command=hang)
    assert time.monotonic() - started < 15
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.parametrize("code, message", [
    ("import sys; sys.stderr.write('the SVG refers to something outside itself'); sys.exit(2)", "outside itself"),
    ("import sys; sys.exit(3)", "memory"),
    ("import sys; sys.exit(1)", "could not be rendered"),
    ("import sys; sys.stdout.write('this is not a png')", "could not be rendered"),
    ("pass", "could not be rendered"),
])
def test_what_the_child_reports_becomes_a_svg_error(code, message):
    with pytest.raises(SvgError, match=message):
        render_svg(OK, command=_python(code))


def test_a_refused_document_never_starts_a_process(tmp_path):
    marker = tmp_path / "ran"
    spy = _python("import sys; open(sys.argv[1], 'w')", str(marker))
    with pytest.raises(SvgError, match="script"):
        render_svg(OK.replace("<rect", "<script/><rect"), command=spy)
    with pytest.raises(SvgError, match="viewBox"):
        render_svg(f"<svg {NS}/>", command=spy)
    assert not marker.exists()
