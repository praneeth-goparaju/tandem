from __future__ import annotations

import asyncio
import struct
from datetime import UTC, datetime

from src.brain.mcp._common import _err, _ok
from src.brain.mcp.caller_context import ToolCaller, require_current_caller
from src.brain.mcp.tool_registry import brain_tool
from src.brain.media.store import MAX_MEDIA_IDS, MEDIA_ID_PATTERN, save_png

MAX_SVG_CHARS = 524_288


def media_ids_schema(where: str) -> dict:
    return {
        "type": "array",
        "items": {"type": "string", "pattern": f"^{MEDIA_ID_PATTERN}$"},
        "maxItems": MAX_MEDIA_IDS,
        "uniqueItems": True,
        "description": (
            f"Media ids from draw_chart or draw_image to post as pictures {where}, in the same "
            f"thread, after the text. At most {MAX_MEDIA_IDS}. An id that does not exist, was "
            "deleted, or was made in another place is left out and named in the answer."
        ),
    }


def caller_place(caller: ToolCaller) -> str:
    """The place a caller's media belong to: its area's place, else private unless the chat is limited to areas."""
    from src.orchestrator.chat import area_place

    place = area_place(caller.area) if caller.area else None
    return place or ("private" if caller.areas is None else "shared")


def _saved(png: bytes, kind: str, caller: ToolCaller) -> dict:
    width, height = struct.unpack(">II", png[16:24])
    run_label = f"{caller.agent}@{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S')}"
    media_id = save_png(
        png, kind=kind, agent=caller.agent, run_label=run_label, area=caller.area,
        place=caller_place(caller),
    )
    return _ok(
        f"media id {media_id}: {kind} {width}x{height} png, {len(png)} bytes. "
        "Pass it in media_ids to post it."
    )


@brain_tool(
    name="draw_chart",
    description=(
        "Draw a line or bar chart as a PNG and get back a media id. `spec` is "
        "{title, kind, x, series, y_label?}: `kind` is `line` or `bar`; `x` is a list of 1 "
        "to 366 text labels of at most 40 characters; `series` is 1 to 6 entries of "
        "{name, values}, with one number in `values` for each label. Numbers and labels "
        "only: a spec with anything else is refused, with the reason. The picture is "
        "1080 by 1350 pixels (portrait, sized for a phone). Pass the media id in `media_ids` of reply_to_message, "
        "push_notify or a send_morning_cards card to post the picture under that "
        "message. A media id works only in the place it was made."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": (
                    "The chart: title (text up to 120 characters), kind (line or bar), "
                    "x (labels), series (name and values) and an optional y_label."
                ),
            },
        },
        "required": ["spec"],
    },
)
async def draw_chart(args):
    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    spec = args.get("spec")
    if not isinstance(spec, dict):
        return _err("spec must be an object")
    from src.brain.media.charts import ChartError, render_chart

    try:
        png = await asyncio.to_thread(render_chart, spec)
    except ChartError as exc:
        return _err(str(exc))
    return _saved(png, "chart", caller)


@brain_tool(
    name="draw_image",
    description=(
        "Draw a picture from SVG text and get back a media id. Give the root `svg` element "
        "a width and height, or a viewBox, of at most 4096 by 4096 pixels; the text may be "
        "up to 512 KB. Shapes, paths, text and `<defs>` are fine. For gradients and "
        "patterns use presentation attributes (`fill=\"url(#g)\"`) or a `<defs>` with local "
        "ids; `url(` inside a `style` element or attribute is refused. Also refused, with "
        "the reason: scripts, `foreignObject`, event handlers, a DOCTYPE, and any `href` "
        "that is not a `#id` or a `data:` URL holding a PNG, JPEG, GIF or WebP picture "
        "(no SVG inside an SVG). Pass the media id in `media_ids` of "
        "reply_to_message, push_notify or a send_morning_cards card to post the picture "
        "under that message. A media id works only in the place it was made."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "svg": {
                "type": "string",
                "maxLength": MAX_SVG_CHARS,
                "description": "The SVG document text.",
            },
        },
        "required": ["svg"],
    },
)
async def draw_image(args):
    try:
        caller = require_current_caller()
    except PermissionError as exc:
        return _err(str(exc))
    svg = args.get("svg")
    if not isinstance(svg, str):
        return _err("svg must be a string")
    from src.brain.media.svg import SvgError, render_svg

    try:
        png = await asyncio.to_thread(render_svg, svg)
    except SvgError as exc:
        return _err(str(exc))
    return _saved(png, "image", caller)
