from __future__ import annotations

import os
import stat
import struct
from pathlib import Path

import pytest

from src.brain.media import charts
from src.brain.media.charts import ChartError, render_chart

GOOD = {"title": "Weight", "kind": "line", "x": ["Jan", "Feb"], "series": [{"name": "kg", "values": [70.0, 71.5]}]}


def _size(png):
    return struct.unpack(">II", png[16:24])


def test_chart_renders_a_1080_by_1350_png():
    png = render_chart(GOOD)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (1080, 1350)


@pytest.mark.parametrize("bad, field", [
    ({**GOOD, "kind": "pie"}, "kind"),
    ({**GOOD, "series": [{"name": "kg", "values": [83.1]}]}, "values"),
    ({**GOOD, "series": [{"name": "kg", "values": ["12", 1]}]}, "values"),
    ({**GOOD, "series": [{"name": "kg", "values": [float("nan"), 1]}]}, "values"),
    ({**GOOD, "series": [{"name": f"s{i}", "values": [1, 2]} for i in range(7)]}, "series"),
    ({**GOOD, "x": [f"d{i}" for i in range(367)], "series": [{"name": "k", "values": list(range(367))}]}, "x"),
    ({**GOOD, "url": "http://x"}, "url"),
    ({**GOOD, "title": "t" * 121}, "title"),
])
def test_chart_refuses_bad_specs(bad, field):
    with pytest.raises(ChartError, match=field):
        render_chart(bad)


@pytest.mark.parametrize("bad, field", [
    ("not an object", "chart"),
    ({k: v for k, v in GOOD.items() if k != "title"}, "title"),
    ({**GOOD, "title": 7}, "title"),
    ({**GOOD, "y_label": "y" * 41}, "y_label"),
    ({**GOOD, "x": []}, "x"),
    ({**GOOD, "x": "JanFeb"}, "x"),
    ({**GOOD, "x": ["Jan", 2]}, "x"),
    ({**GOOD, "x": ["a" * 41, "b"]}, "x"),
    ({**GOOD, "series": []}, "series"),
    ({**GOOD, "series": ["kg"]}, "series"),
    ({**GOOD, "series": [{"values": [1, 2]}]}, "name"),
    ({**GOOD, "series": [{"name": "n" * 41, "values": [1, 2]}]}, "name"),
    ({**GOOD, "series": [{"name": "kg", "values": "12"}]}, "values"),
    ({**GOOD, "series": [{"name": "kg", "values": [True, 2]}]}, "values"),
    ({**GOOD, "series": [{"name": "kg", "values": [float("inf"), 2]}]}, "values"),
    ({**GOOD, "series": [{"name": "kg", "values": [10**400, 2]}]}, "values"),
    ({**GOOD, "series": [{"name": "kg", "values": [1, 2], "color": "red"}]}, "color"),
])
def test_chart_refuses_every_other_malformed_part(bad, field):
    with pytest.raises(ChartError, match=field):
        render_chart(bad)


def test_the_largest_allowed_spec_renders_for_both_kinds():
    spec = {
        "title": "t" * 120, "y_label": "y" * 40, "x": [f"{i:03d}".ljust(40, "L") for i in range(366)],
        "series": [{"name": f"{i}".ljust(40, "n"), "values": [float(i + j) for j in range(366)]} for i in range(6)],
    }
    assert _size(render_chart({**spec, "kind": "line"})) == (1080, 1350)
    assert _size(render_chart({**spec, "kind": "bar"})) == (1080, 1350)


def test_a_single_point_and_integer_values_render():
    spec = {"title": "One", "kind": "bar", "x": ["only"], "series": [{"name": "n", "values": [5]}]}
    assert _size(render_chart(spec)) == (1080, 1350)


def test_labels_are_drawn_as_text_and_never_read_as_math():
    spec = {
        "title": r"$\frac{$", "kind": "line", "x": [r"$\frac{$", "b"], "y_label": r"$\sqrt{$",
        "series": [{"name": r"$\left($", "values": [1, 2]}],
    }
    assert _size(render_chart(spec)) == (1080, 1350)


def test_renders_share_no_state_and_leave_no_figures_open():
    import matplotlib
    import matplotlib.pyplot as plt

    other = {**GOOD, "kind": "bar", "series": [{"name": "a", "values": [1, 2]}, {"name": "b", "values": [3, 4]}]}
    first = render_chart(GOOD)
    render_chart(other)
    assert render_chart(GOOD) == first
    assert plt.get_fignums() == []
    assert matplotlib.get_backend().lower() == "agg"


def test_the_refusal_does_not_echo_a_long_unknown_field():
    with pytest.raises(ChartError) as refusal:
        render_chart({**GOOD, "z" * 500: 1})
    assert len(str(refusal.value)) < 200


@pytest.mark.filterwarnings("ignore:overflow encountered:RuntimeWarning")
@pytest.mark.parametrize("kind", ["line", "bar"])
def test_values_matplotlib_cannot_draw_are_refused_as_a_chart_error(kind):
    spec = {**GOOD, "kind": kind, "series": [{"name": "kg", "values": [1.7e308, 1.7e308]}]}
    with pytest.raises(ChartError, match="values"):
        render_chart(spec)


def test_the_owners_matplotlib_settings_do_not_change_the_chart(monkeypatch):
    import matplotlib

    plain = render_chart(GOOD)
    owners = {
        "savefig.bbox": "tight", "savefig.pad_inches": 2.0, "figure.dpi": 50.0, "lines.linewidth": 9.0,
        "axes.facecolor": "black", "font.size": 30.0, "axes.grid": False, "text.usetex": True,
    }
    for key, value in owners.items():
        monkeypatch.setitem(matplotlib.rcParams, key, value)

    assert render_chart(GOOD) == plain
    assert {key: matplotlib.rcParams[key] for key in owners} == owners


def test_matplotlib_gets_a_private_config_folder_of_its_own_when_none_is_set(tmp_path):
    environ: dict[str, str] = {}
    charts.use_private_config_folder(environ, tmp_path)
    folder = Path(environ["MPLCONFIGDIR"])
    assert folder == tmp_path / f"brain-matplotlib-{os.getuid()}"
    assert folder.is_dir() and stat.S_IMODE(folder.lstat().st_mode) == 0o700
    again: dict[str, str] = {"MPLCONFIGDIR": " "}
    charts.use_private_config_folder(again, tmp_path)
    assert again == environ


def test_a_config_folder_that_is_set_is_kept(tmp_path):
    environ = {"MPLCONFIGDIR": "/somewhere/else"}
    charts.use_private_config_folder(environ, tmp_path)
    assert environ == {"MPLCONFIGDIR": "/somewhere/else"} and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("taken", ["open-folder", "symlink", "file"])
def test_a_folder_name_taken_by_anything_else_gets_a_fresh_private_folder(tmp_path, taken):
    name = tmp_path / f"brain-matplotlib-{os.getuid()}"
    if taken == "open-folder":
        name.mkdir()
        os.chmod(name, 0o755)
    elif taken == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        name.symlink_to(tmp_path / "elsewhere")
    else:
        name.write_text("not a folder")
    environ: dict[str, str] = {}
    charts.use_private_config_folder(environ, tmp_path)
    folder = Path(environ["MPLCONFIGDIR"])
    assert folder != name and folder.parent == tmp_path and folder.name.startswith("brain-matplotlib-")
    assert not folder.is_symlink() and stat.S_IMODE(folder.lstat().st_mode) == 0o700
