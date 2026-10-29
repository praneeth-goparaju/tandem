"""The launchd templates and the renderer that fills them in."""

import os
import plistlib
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.brain.connector import launchd
from src.brain.connector.launchd import render_launchd

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "deploy" / "launchd"

CONNECTOR = "vault-connector"
CLOUDFLARED = "cloudflared"
HEALTH = "connector-health"
SNAPSHOT = "vault-snapshot"
BACKUP = "local-backup"
GARMIN = "garmin-sync"

HOME = Path("/srv/sample")
REPO_ROOT = HOME / "brain"
USER_DIR = REPO_ROOT / "user"
VAULT = HOME / "Documents" / "My Vault & Notes"
ZONE = "America/New_York"


def render(tmp_path: Path, **overrides) -> dict[str, dict]:
    arguments = {
        "repo_root": REPO_ROOT,
        "home": HOME,
        "user_dir": USER_DIR,
        "out_dir": tmp_path / "out",
        "timezone": ZONE,
    }
    arguments.update(overrides)
    written = render_launchd(**arguments)
    jobs = {}
    for path in written:
        with path.open("rb") as handle:
            jobs[path.name.removesuffix(".plist")] = plistlib.load(handle)
    return jobs


def test_only_the_six_templates_ship_and_no_rendered_job():
    assert len(sorted(TEMPLATES.glob("*.plist.example"))) == 6
    assert not list(TEMPLATES.glob("*.plist"))


def test_render_substitutes_everything(tmp_path):
    assert "&" in str(VAULT)
    written = render_launchd(
        REPO_ROOT, HOME, USER_DIR, tmp_path / "out", "com.sample", vault_path=VAULT, timezone=ZONE
    )
    assert sorted(path.name for path in written) == [
        f"com.sample.{name}.plist" for name in sorted([CLOUDFLARED, HEALTH, CONNECTOR, SNAPSHOT, BACKUP, GARMIN])
    ]
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "__" not in text
        assert "com.example" not in text.split("-->", 1)[1]
        job = plistlib.loads(text.encode("utf-8"))
        assert job["Label"] == path.name.removesuffix(".plist")
        assert job["Label"].startswith("com.sample.")
        assert job["StandardOutPath"].startswith(f"{HOME}/connector-data/logs/")
    jobs = {path.name: plistlib.loads(path.read_bytes()) for path in written}
    connector = jobs[f"com.sample.{CONNECTOR}.plist"]
    assert connector["ProgramArguments"] == [
        f"{REPO_ROOT}/.venv/bin/python",
        "-m",
        "src.brain.connector",
        "serve",
    ]
    assert connector["WorkingDirectory"] == str(REPO_ROOT)
    assert connector["KeepAlive"] is True
    assert connector["RunAtLoad"] is True
    health = jobs[f"com.sample.{HEALTH}.plist"]
    assert health["ProgramArguments"] == [
        f"{REPO_ROOT}/.venv/bin/python",
        f"{REPO_ROOT}/scripts/connector_health.py",
    ]
    assert health["StartInterval"] == 300
    assert "KeepAlive" not in health
    tunnel = jobs[f"com.sample.{CLOUDFLARED}.plist"]
    assert tunnel["ProgramArguments"][1:] == ["tunnel", "run", "vault"]
    assert tunnel["EnvironmentVariables"]["HOME"] == str(HOME)
    snapshot = jobs[f"com.sample.{SNAPSHOT}.plist"]
    assert snapshot["ProgramArguments"] == [
        "/bin/bash",
        f"{REPO_ROOT}/scripts/vault_snapshot.sh",
        str(VAULT),
        f"{HOME}/brain-vault.git",
    ]
    assert snapshot["StartCalendarInterval"] == {"Hour": 23, "Minute": 30}


def test_snapshot_is_skipped_without_a_vault_path(tmp_path):
    jobs = render(tmp_path)
    assert sorted(jobs) == sorted(f"com.example.{name}" for name in (CONNECTOR, CLOUDFLARED, HEALTH, BACKUP, GARMIN))


def test_rendered_env_has_timezone_and_user_dir(tmp_path):
    jobs = render(tmp_path)
    for name in (CONNECTOR, HEALTH, BACKUP, GARMIN):
        env = jobs[f"com.example.{name}"]["EnvironmentVariables"]
        assert env["USER_DIR"] == str(USER_DIR)
        assert env["BRAIN_DIR"] == str(REPO_ROOT)
        assert env["HOME"] == str(HOME)
        assert env["TZ"] == ZONE
        assert env["TIMEZONE"] == ZONE
        assert "/opt/homebrew/bin" in env["PATH"].split(":")


def test_every_log_is_under_the_connector_log_folder(tmp_path):
    jobs = render(tmp_path, vault_path=VAULT)
    log_dir = f"{HOME}/connector-data/logs/"
    assert len(jobs) == 6
    for name, job in jobs.items():
        assert job["StandardOutPath"].startswith(log_dir), name
        assert job["StandardErrorPath"].startswith(log_dir), name


def test_render_writes_into_a_private_folder_by_default(tmp_path):
    user_dir = tmp_path / "overlay"
    written = render_launchd(REPO_ROOT, HOME, user_dir, None, timezone=ZONE)
    folder = user_dir / "launchd"
    assert {path.parent for path in written} == {folder}
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_render_creates_the_output_folder_private(tmp_path):
    out = tmp_path / "a" / "out"
    render_launchd(REPO_ROOT, HOME, USER_DIR, out, timezone=ZONE)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_render_refuses_leftover_placeholder(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "com.example.a-good.plist.example").write_text(
        "<plist><string>__HOME__</string></plist>", encoding="utf-8"
    )
    (templates / "com.example.sample.plist.example").write_text(
        "<plist><string>__UNKNOWN__</string><string>__HOME__</string></plist>",
        encoding="utf-8",
    )
    monkeypatch.setattr(launchd, "TEMPLATE_DIR", templates)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="__UNKNOWN__"):
        render_launchd(REPO_ROOT, HOME, USER_DIR, out, timezone=ZONE)
    assert not out.exists() or not list(out.iterdir())


def test_render_replaces_an_existing_file(tmp_path):
    out = tmp_path / "out"
    render_launchd(REPO_ROOT, HOME, USER_DIR, out, timezone=ZONE)
    stale = out / f"com.example.{CONNECTOR}.plist"
    stale.write_text("old", encoding="utf-8")
    render_launchd(REPO_ROOT, HOME, USER_DIR, out, timezone=ZONE)
    assert plistlib.loads(stale.read_bytes())["Label"] == f"com.example.{CONNECTOR}"


@pytest.mark.skipif(sys.platform != "darwin", reason="plutil is a macOS tool")
def test_rendered_files_pass_plutil(tmp_path):
    written = render_launchd(
        REPO_ROOT, HOME, USER_DIR, tmp_path / "out", "com.sample", vault_path=VAULT, timezone=ZONE
    )
    for path in written:
        result = subprocess.run(
            ["plutil", "-lint", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr


def run_cli(*args: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "USER_DIR": str(tmp_path / "overlay"), "HOME": str(tmp_path / "home")}
    return subprocess.run(
        [sys.executable, "-m", "src.cli", "render-launchd", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_prints_the_copy_and_the_bootstrap_from_launch_agents(tmp_path):
    out = tmp_path / "out"
    result = run_cli("--label-prefix", "com.sample", "--out", str(out), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'mkdir -p "$HOME/Library/LaunchAgents"' in result.stdout
    for name in (CONNECTOR, CLOUDFLARED, HEALTH):
        path = out / f"com.sample.{name}.plist"
        assert path.is_file()
        assert f'cp {path} "$HOME/Library/LaunchAgents/"' in result.stdout
        assert (
            f'launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.sample.{name}.plist"'
            in result.stdout
        )
    assert f"bootstrap gui/$(id -u) {out}" not in result.stdout
    assert "kickstart" not in result.stdout
    assert SNAPSHOT not in result.stdout


def test_cli_defaults_to_the_overlay_folder_and_renders_the_snapshot(tmp_path):
    vault = tmp_path / "vault"
    result = run_cli("--vault-path", str(vault), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    folder = tmp_path / "overlay" / "launchd"
    assert (folder / f"com.example.{SNAPSHOT}.plist").is_file()
    assert str(folder / f"com.example.{CONNECTOR}.plist") in result.stdout


def test_rendered_files_say_they_were_rendered(tmp_path):
    written = render_launchd(REPO_ROOT, HOME, USER_DIR, tmp_path / "out", vault_path=VAULT, timezone=ZONE)
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "Do not load" not in text
        assert "render-launchd" in text
        assert "deploy/launchd/com.example." in text
        assert ".plist.example" in text


def test_values_are_never_rewritten_by_the_fill(tmp_path):
    odd_home = Path("/srv/com.example/__USER_DIR__")
    written = render_launchd(
        REPO_ROOT, odd_home, USER_DIR, tmp_path / "out", "com.sample", vault_path=VAULT,
        timezone=ZONE,
    )
    for path in written:
        job = plistlib.loads(path.read_bytes())
        assert job["StandardOutPath"].startswith(f"{odd_home}/connector-data/logs/")
    snapshot = plistlib.loads((tmp_path / "out" / f"com.sample.{SNAPSHOT}.plist").read_bytes())
    assert snapshot["ProgramArguments"][3] == f"{odd_home}/brain-vault.git"


def test_cli_quotes_paths_with_spaces(tmp_path):
    env = {**os.environ, "USER_DIR": str(tmp_path / "my overlay"), "HOME": str(tmp_path / "home")}
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "render-launchd"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    path = tmp_path / "my overlay" / "launchd" / f"com.example.{CONNECTOR}.plist"
    assert f'cp {shlex.quote(str(path))} "$HOME/Library/LaunchAgents/"' in result.stdout


def test_the_templates_name_no_zone_and_every_job_gets_the_given_one(tmp_path):
    for template in TEMPLATES.glob("*.plist.example"):
        text = template.read_text(encoding="utf-8")
        assert "Europe/" not in text and "America/" not in text
    jobs = render(tmp_path, vault_path=VAULT)
    with_env = {name: job["EnvironmentVariables"] for name, job in jobs.items()
                if "EnvironmentVariables" in job}
    assert len(with_env) == 5
    for name, env in with_env.items():
        assert env["TZ"] == ZONE, name
        assert env.get("TIMEZONE", ZONE) == ZONE, name


def _overlay_with(tmp_path: Path, profile: str, connector_extra: str = "") -> Path:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "profile.yaml").write_text(profile, encoding="utf-8")
    (overlay / "vault_connector.yaml").write_text(
        f"vault_path: {tmp_path}\ndata_dir: {tmp_path / 'data'}\n"
        f"public_url: https://vault.example.com\n{connector_extra}",
        encoding="utf-8",
    )
    return overlay


def test_cli_fills_the_zone_from_the_profile(tmp_path):
    _overlay_with(tmp_path, "user:\n  timezone: America/New_York\n")
    result = run_cli("--out", str(tmp_path / "out"), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    job = plistlib.loads((tmp_path / "out" / f"com.example.{CONNECTOR}.plist").read_bytes())
    assert job["EnvironmentVariables"]["TZ"] == "America/New_York"
    assert job["EnvironmentVariables"]["TIMEZONE"] == "America/New_York"
    assert "America/New_York (from profile.yaml)" in result.stdout


def test_cli_prefers_the_connector_zone(tmp_path):
    _overlay_with(tmp_path, "user:\n  timezone: America/New_York\n", "timezone: Asia/Tokyo\n")
    result = run_cli("--out", str(tmp_path / "out"), tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    job = plistlib.loads((tmp_path / "out" / f"com.example.{HEALTH}.plist").read_bytes())
    assert job["EnvironmentVariables"]["TZ"] == "Asia/Tokyo"


def test_backup_and_garmin_jobs_run_after_the_daemon_times(tmp_path):
    jobs = render(tmp_path)
    backup = jobs[f"com.example.{BACKUP}"]
    garmin = jobs[f"com.example.{GARMIN}"]
    assert backup["ProgramArguments"] == [f"{REPO_ROOT}/.venv/bin/python", "-m", "src.orchestrator.local_backup"]
    assert garmin["ProgramArguments"] == [f"{REPO_ROOT}/.venv/bin/python", "-m", "src.orchestrator.garmin_sync"]
    assert backup["StartCalendarInterval"] == {"Hour": 23, "Minute": 50}
    assert garmin["StartCalendarInterval"] == {"Hour": 7, "Minute": 10}
