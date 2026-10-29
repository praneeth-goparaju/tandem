from __future__ import annotations

import math
import os
import stat
import tempfile
from collections.abc import Mapping, MutableMapping
from io import BytesIO
from pathlib import Path
from typing import Any


def use_private_config_folder(environ: MutableMapping[str, str], temp_root: Path) -> None:
    if (environ.get("MPLCONFIGDIR") or "").strip():
        return
    folder = temp_root / f"brain-matplotlib-{os.getuid()}"
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        info = folder.lstat()
        private = (
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
        )
    except OSError:
        private = False
    if not private:
        folder = Path(tempfile.mkdtemp(prefix="brain-matplotlib-", dir=temp_root))
    environ["MPLCONFIGDIR"] = str(folder)


use_private_config_folder(os.environ, Path(tempfile.gettempdir()))

import matplotlib  # noqa: E402
import matplotlib.patches  # noqa: E402
import matplotlib.ticker  # noqa: E402
import matplotlib.transforms  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

matplotlib.use("Agg")

MAX_TITLE = 120
MAX_LABEL = 40
MAX_POINTS = 366
MAX_SERIES = 6
KINDS = ("line", "bar")
_FIELDS = ("title", "kind", "x", "series", "y_label")
_SERIES_FIELDS = ("name", "values")


class ChartError(ValueError):
    ...


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ChartError(f"{field} must be text of at most {limit} characters")
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _validate(spec: Mapping[str, Any]) -> tuple[str, str, list[str], list[tuple[str, list[float]]], str]:
    if not isinstance(spec, Mapping):
        raise ChartError("a chart is an object with title, kind, x and series")
    for key in spec:
        if key not in _FIELDS:
            raise ChartError(f"{str(key)[:40]!r} is not a chart field; use {', '.join(_FIELDS)}")
    for field in ("title", "kind", "x", "series"):
        if field not in spec:
            raise ChartError(f"{field} is required")

    title = _text(spec["title"], "title", MAX_TITLE)
    kind = spec["kind"]
    if kind not in KINDS:
        raise ChartError("kind must be 'line' or 'bar'")

    labels = spec["x"]
    if not isinstance(labels, list) or not 1 <= len(labels) <= MAX_POINTS:
        raise ChartError(f"x must be a list of 1 to {MAX_POINTS} labels")
    for label in labels:
        _text(label, "x labels", MAX_LABEL)

    entries = spec["series"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SERIES:
        raise ChartError(f"series must be a list of 1 to {MAX_SERIES} entries")
    series: list[tuple[str, list[float]]] = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, Mapping):
            raise ChartError(f"series {number} must be an object with a name and values")
        for key in entry:
            if key not in _SERIES_FIELDS:
                raise ChartError(f"series {number} has {str(key)[:40]!r}; use name and values")
        if "name" not in entry:
            raise ChartError(f"series {number} needs a name")
        name = _text(entry["name"], f"series {number} name", MAX_LABEL)
        values = entry.get("values")
        if not isinstance(values, list):
            raise ChartError(f"values of series {number} must be a list of numbers")
        if len(values) != len(labels):
            raise ChartError(
                f"values of series {number} must have one number per x label "
                f"({len(labels)}), not {len(values)}"
            )
        numbers = [_number(value) for value in values]
        if None in numbers:
            raise ChartError(
                f"values of series {number} must be finite numbers; item {numbers.index(None)} is not"
            )
        series.append((name, [n for n in numbers if n is not None]))

    y_label = _text(spec["y_label"], "y_label", MAX_LABEL) if "y_label" in spec else ""
    return title, kind, labels, series, y_label


def _literal(text: str) -> str:
    return text.replace("$", r"\$")


WIDTH, HEIGHT, DPI = 1080, 1350, 100
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#8a8985"
GRID = "#e6e5e1"
PALETTE = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
_MAX_X_LABELS = 7
_LINE_WIDTH = 6
_MARKER_SIZE = 16


def render_chart(spec: Mapping[str, Any]) -> bytes:
    """PNG bytes, 1080 by 1350 (portrait, for a phone), of a `line` or `bar` chart.

    Raises ChartError for a bad spec, and for values the plotting library
    cannot draw.
    """
    title, kind, labels, series, y_label = _validate(spec)
    try:
        with matplotlib.rc_context(rc=matplotlib.rcParamsDefault):
            return _draw(title, kind, labels, series, y_label)
    except Exception as exc:
        raise ChartError("the chart could not be drawn from these values") from exc


def _x_positions(count: int) -> list[int]:
    if count <= _MAX_X_LABELS:
        return list(range(count))
    step = math.ceil((count - 1) / (_MAX_X_LABELS - 1))
    ticks = list(range(0, count, step))
    if ticks[-1] != count - 1:
        if count - 1 - ticks[-1] < step * 0.6:
            ticks[-1] = count - 1
        else:
            ticks.append(count - 1)
    return ticks


def _decimals(values: list[float]) -> int:
    for digits in (0, 1, 2):
        if all(abs(v * 10**digits - round(v * 10**digits)) < 1e-6 for v in values):
            return digits
    return 2


def _format(value: float, digits: int) -> str:
    return f"{value:,.{digits}f}".replace(",", "\u2009")


def _draw(
    title: str, kind: str, labels: list[str], series: list[tuple[str, list[float]]], y_label: str,
) -> bytes:
    figure = Figure(figsize=(WIDTH / DPI, HEIGHT / DPI), dpi=DPI, facecolor=SURFACE)
    figure.text(0.07, 0.955, _literal(title), fontsize=34, fontweight="bold", color=INK,
                ha="left", va="top", wrap=True)
    if y_label:
        figure.text(0.07, 0.905, _literal(y_label), fontsize=22, color=INK_SECONDARY, ha="left", va="top")
    many = len(series) > 1
    if many:
        renderer = FigureCanvasAgg(figure).get_renderer()
        row_y, x, gap = 0.865, 0.07, 0.028
        for index, (name, _values) in enumerate(series):
            label = figure.text(0, 0, _literal(name), fontsize=21, color=INK_SECONDARY, va="center")
            width = label.get_window_extent(renderer).width / WIDTH
            if x + 0.04 + width > 0.93 and x > 0.07:
                row_y, x = row_y - 0.034, 0.07
            figure.patches.append(matplotlib.patches.FancyBboxPatch(
                (x, row_y - 0.0075), 0.028, 0.015, boxstyle="round,pad=0,rounding_size=0.006",
                transform=figure.transFigure, facecolor=PALETTE[index], edgecolor="none",
            ))
            label.set_position((x + 0.038, row_y))
            x += 0.038 + width + gap
        top = row_y - 0.045
    else:
        top = 0.865 if y_label else 0.89
    axes = figure.add_axes((0.07, 0.11, 0.80, top - 0.11), facecolor=SURFACE)
    positions = list(range(len(labels)))
    every = [v for _name, values in series for v in values]
    low, high = min(every + ([0.0] if kind == "bar" else [])), max(every + ([0.0] if kind == "bar" else []))
    span = (high - low) or abs(high) or 1.0
    data_digits = _decimals(every)

    if kind == "line":
        marker = "o" if len(labels) <= 24 else None
        line_width = _LINE_WIDTH if len(labels) <= 40 else 3.5
        for index, (_name, values) in enumerate(series):
            color = PALETTE[index]
            axes.plot(positions, values, color=color, linewidth=line_width, solid_capstyle="round",
                      solid_joinstyle="round", marker=marker, markersize=_MARKER_SIZE,
                      markerfacecolor=color, markeredgecolor=SURFACE, markeredgewidth=3, zorder=3)
            axes.plot([positions[-1]], [values[-1]], "o", color=color, markersize=_MARKER_SIZE + 6,
                      markeredgecolor=SURFACE, markeredgewidth=4, zorder=4)
        ends = sorted(((values[-1], index) for index, (_n, values) in enumerate(series)))
        placed: list[float] = []
        min_gap = span * 0.07
        beside = matplotlib.transforms.offset_copy(axes.transData, fig=figure, x=16, y=0, units="points")
        for value, _index in ends:
            y = value
            if placed and y - placed[-1] < min_gap:
                y = placed[-1] + min_gap
            placed.append(y)
            axes.text(positions[-1], y, _format(value, data_digits), transform=beside, fontsize=24,
                      fontweight="bold", color=INK, va="center", ha="left", clip_on=False)
        axes.set_xlim(-0.3 if len(labels) > 1 else -1, positions[-1] + (0.3 if len(labels) > 1 else 1))
    else:
        group = 0.78
        width = group / len(series)
        for index, (_name, values) in enumerate(series):
            shift = (index - (len(series) - 1) / 2) * width
            axes.bar([p + shift for p in positions], values, width=width * 0.92, color=PALETTE[index],
                     edgecolor=SURFACE, linewidth=2, zorder=3)
        if len(series) == 1 and len(labels) <= 8:
            for p, value in zip(positions, series[0][1], strict=True):
                axes.annotate(_format(value, data_digits), (p, value), xytext=(0, 8 if value >= 0 else -8),
                              textcoords="offset points", fontsize=22, fontweight="bold", color=INK,
                              ha="center", va="bottom" if value >= 0 else "top", annotation_clip=False)
        axes.axhline(0, color=INK_MUTED, linewidth=2, zorder=4)
        axes.set_xlim(-0.6, positions[-1] + 0.6)

    pad = span * 0.12
    bottom = 0 if kind == "bar" and low >= 0 else low - pad
    axes.set_ylim(bottom, high + pad)
    locator = matplotlib.ticker.MaxNLocator(nbins=4, min_n_ticks=3)
    axes.yaxis.set_major_locator(locator)
    ticks = [t for t in locator.tick_values(bottom, high + pad) if bottom <= t <= high + pad]
    tick_digits = _decimals(ticks) if ticks else data_digits
    axes.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: _format(v, tick_digits)))
    axes.yaxis.tick_right()
    shown = _x_positions(len(labels))
    axes.set_xticks(shown, labels=[_literal(labels[p]) for p in shown])
    axes.tick_params(axis="both", length=0, labelsize=21, colors=INK_SECONDARY, pad=12)
    axes.grid(axis="y", color=GRID, linewidth=2, zorder=0)
    axes.grid(axis="x", visible=False)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(GRID if kind == "line" else SURFACE)
    axes.spines["bottom"].set_linewidth(2)
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=DPI, facecolor=SURFACE)
    return buffer.getvalue()
