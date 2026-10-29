from __future__ import annotations

import base64
import binascii
import logging
import math
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote_to_bytes

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

log = logging.getLogger(__name__)

MAX_BYTES = 524_288
MAX_SIDE = 4096
EXIT_REFUSED = 2
EXIT_MEMORY = 3
CHILD_COMMAND = (sys.executable, "-I", "-m", "src.brain.media.svg_render")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RASTER_MAGIC = (_PNG_MAGIC, b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
_OUTSIDE = "the SVG refers to something outside itself; an href may only be a data: URL or a #id"
_REFUSED_ELEMENTS = ("script", "foreignobject")
_FONT_PX = 16.0
_UNITS = {"mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pt": 96 / 72, "pc": 16.0, "px": 1.0}


class SvgError(ValueError):
    ...


def _local(name: Any) -> str:
    return str(name).rpartition("}")[2]


def _reaches_out(css: str) -> bool:
    lowered = css.lower()
    return "url(" in lowered or "@import" in lowered


def _data_url_denial(url: str) -> str | None:
    """The renderer decides from a picture's bytes, not its declared type, whether it is
    another SVG, so the bytes are decoded as the renderer's fetcher decodes them: tabs
    and line breaks dropped, percent escapes undone, and base64 decoded when the type
    ends in `;base64`.
    """
    header, _, payload = re.sub("[\t\r\n]", "", url)[len("data:"):].partition(",")
    if header.split(";", 1)[0].strip().lower() == "image/svg+xml":
        return "the SVG holds another SVG in a data: URL"
    try:
        data = unquote_to_bytes(payload)
        if header.endswith(";base64"):
            data = base64.decodebytes(data)
    except (binascii.Error, ValueError):
        data = b""
    if data.startswith(_RASTER_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return None
    return "a data: URL in the SVG may hold only a PNG, JPEG, GIF or WebP picture"


def _href_denial(value: str) -> str | None:
    if "url(" in value.lower():
        return _OUTSIDE
    if value.startswith("#"):
        return None
    if value.startswith("data:"):
        return _data_url_denial(value)
    return _OUTSIDE


def _normalize(value: str) -> str:
    value = value.replace("E", "e")
    value = re.sub("(?<!e)-", " -", value)
    value = re.sub("[ \n\r\t,]+", " ", value)
    value = re.sub(r"(\.[0-9-]+)(?=\.)", r"\1 ", value)
    return value.strip()


def _length(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        pass
    token = _normalize(value).split(" ", 1)[0]
    try:
        if token.endswith("%"):
            return 0.0
        if token.endswith("em"):
            return _FONT_PX * float(token[:-2])
        if token.endswith(("ex", "ch")):
            return _FONT_PX * float(token[:-2]) / 2
        for unit, factor in _UNITS.items():
            if token.endswith(unit):
                return float(token[: -len(unit)]) * factor
    except ValueError:
        return 0.0
    return 0.0


def _view_box(value: str | None) -> tuple[float, ...] | None:
    if not value:
        return None
    try:
        box = tuple(float(part) for part in re.sub("[ \n\r\t,]+", " ", value).split())
    except ValueError:
        return None
    return box if len(box) >= 4 else None


def _natural_size(root: Any) -> tuple[float, float] | None:
    width, height = _length(root.get("width")), _length(root.get("height"))
    box = _view_box(root.get("viewBox"))
    if box:
        width, height = width or box[2], height or box[3]
    return (width, height) if 0 < width < math.inf and 0 < height < math.inf else None


def _parse(text: str) -> Any:
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SvgError("the SVG is not valid text") from exc
    if len(data) > MAX_BYTES:
        raise SvgError("the SVG is larger than 512 KB")
    try:
        return ElementTree.fromstring(
            data, forbid_dtd=True, forbid_entities=True, forbid_external=True,
        )
    except (ElementTree.ParseError, DefusedXmlException, LookupError, ValueError) as exc:
        raise SvgError(
            "the SVG is not well-formed XML, or it has a DOCTYPE, an entity or an external reference"
        ) from exc


def check_svg(text: str) -> None:
    """Raise SvgError unless `text` is a plain SVG document.

    Refused: text over 512 KB; anything that does not parse, or has a DOCTYPE,
    an entity or an external reference; a `script` or `foreignObject` element;
    an `href` that is not a `#id` or a `data:` URL holding a PNG, JPEG, GIF or
    WebP picture, so no SVG holds another; an `href` with `url(` in it; `url(` or
    `@import` in a `style` element or attribute; an event-handler attribute; and
    a root `width`, `height` or `viewBox` over 4096 pixels.
    """
    root = _parse(text)
    for element in root.iter():
        name = _local(element.tag)
        if name.lower() in _REFUSED_ELEMENTS:
            raise SvgError(f"the SVG has a <{name}> element")
        if name.lower() == "style" and _reaches_out("".join(element.itertext())):
            raise SvgError("a <style> element refers to a URL or imports a stylesheet")
        for key, value in element.attrib.items():
            attribute = _local(key).lower()
            if attribute.startswith("on"):
                raise SvgError(f"the SVG has an event-handler attribute, {attribute}")
            reason = _href_denial(value) if attribute == "href" else None
            if reason:
                raise SvgError(reason)
            if attribute == "style" and _reaches_out(value):
                raise SvgError("a style attribute refers to a URL or imports a stylesheet")
    sides = [_length(root.get("width")), _length(root.get("height"))]
    box = _view_box(root.get("viewBox"))
    if box:
        sides += box[2:4]
    if any(not math.isfinite(side) or side > MAX_SIDE for side in sides):
        raise SvgError(f"the SVG is larger than {MAX_SIDE} by {MAX_SIDE} pixels")


def output_size(text: str) -> tuple[int, int]:
    """The pixel size a render of `text` gets: its own size, shrunk to fit 4096 by 4096.

    Raises SvgError when the document has no width and height or viewBox.
    """
    size = _natural_size(_parse(text))
    if size is None:
        raise SvgError("the SVG needs a width and height, or a viewBox")
    shrink = min(1.0, MAX_SIDE / max(size))
    width, height = (max(1, round(side * shrink)) for side in size)
    return width, height


def render_svg(text: str, *, timeout: float = 10.0, command: Sequence[str] | None = None) -> bytes:
    """PNG bytes of a checked SVG, never larger than 4096 by 4096 pixels.

    Raises SvgError when the check refuses the document, when it has no size,
    when the render fails or needs too much memory, or after `timeout` seconds,
    when the child process is killed. `command` replaces the child's command line.
    """
    check_svg(text)
    output_size(text)
    environ = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    environ.setdefault("PATH", os.defpath)
    try:
        done = subprocess.run(  # noqa: S603
            list(command or CHILD_COMMAND),
            input=text.encode("utf-8"), capture_output=True, timeout=timeout, env=environ,
        )
    except subprocess.TimeoutExpired:
        log.warning("an SVG render ran past %s s and was killed", timeout)
        raise SvgError("render timed out") from None
    if done.returncode == EXIT_REFUSED:
        reason = done.stderr.decode("utf-8", "replace").strip()[:200]
        raise SvgError(reason or "the SVG was refused")
    if done.returncode == EXIT_MEMORY:
        raise SvgError("the SVG needs more memory than a render may use")
    if done.returncode != 0 or not done.stdout.startswith(_PNG_MAGIC):
        log.warning(
            "an SVG render exited %s: %s", done.returncode,
            done.stderr.decode("utf-8", "replace").strip()[-300:],
        )
        raise SvgError("the SVG could not be rendered")
    return done.stdout
