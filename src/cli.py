from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        load_dotenv(_REPO_ROOT / ".env")
        return _run_doctor(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "render-launchd":
        return _run_render_launchd(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "init-vault":
        return _run_init_vault(sys.argv[2:])

    parser = argparse.ArgumentParser(description="Brain Agents CLI")
    parser.add_argument(
        "--run",
        type=str,
        required=True,
        help="A mode from config/modes.yaml (all_morning, scout, *_solo) or a single agent name",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would run without executing",
    )
    args = parser.parse_args()

    load_dotenv(_REPO_ROOT / ".env")
    print(f"[{datetime.now().isoformat()}] tandem: --run {args.run}")

    from src.orchestrator.runner import AGENT_REGISTRY, RUN_MODES

    if args.dry_run:
        if args.run in RUN_MODES:
            mode = RUN_MODES[args.run]
            print(f"DRY RUN mode: {args.run}")
            if mode.get("pre"):
                print(f"  Pre: {mode['pre']}")
            print(f"  Parallel: {mode['parallel']}")
            print(f"  Sequential: {mode['sequential']}")
            if mode.get("chain"):
                print(f"  Chain: → {mode['chain']}")
            for name in mode.get("pre", []) + mode["parallel"] + mode["sequential"]:
                cfg = AGENT_REGISTRY[name]
                print(f"    {name}: model={cfg['model']}, tools={len(cfg['tools'])}")
        elif args.run in AGENT_REGISTRY:
            cfg = AGENT_REGISTRY[args.run]
            print(f"DRY RUN agent: {args.run}")
            print(f"  Model: {cfg['model']}")
            print(f"  Tools: {cfg['tools']}")
        else:
            print(f"Unknown: {args.run}")
            sys.exit(1)
        return

    asyncio.run(_run_orchestrated(args.run))


def _run_doctor(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli doctor",
        description="Validate the merged config + agent registry.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Promote warnings to errors (use in CI to block drift).",
    )
    parser.add_argument(
        "--connector",
        action="store_true",
        help="Run only the vault connector's checks, for an install without the agent daemon; any warning fails.",
    )
    args = parser.parse_args(argv)

    from src.brain.doctor import print_report, run_checks

    print(f"[{datetime.now().isoformat()}] tandem doctor — {_REPO_ROOT}")
    print()
    strict = args.strict or args.connector
    results, exit_code = run_checks(_REPO_ROOT, strict=strict, connector_only=args.connector)
    print_report(results, strict=strict)
    sys.exit(exit_code)


def _run_render_launchd(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli render-launchd",
        description="Write the launchd jobs of the vault connector, with your paths filled in.",
    )
    parser.add_argument(
        "--label-prefix",
        default="com.example",
        help="Start of every job name, for example com.yourname (default: com.example).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Folder to write to (default: the launchd folder in your private folder).",
    )
    parser.add_argument(
        "--vault-path",
        type=Path,
        default=None,
        help="Your vault folder. Needed for the nightly snapshot job, which is left out without it.",
    )
    args = parser.parse_args(argv)

    import yaml

    from src.brain.connector.config import (
        DEFAULT_CONFIG_PATH,
        PROFILE_NAME,
        load_config,
        resolve_timezone,
        user_dir,
    )
    from src.brain.connector.launchd import render_launchd

    try:
        settings = user_dir() / DEFAULT_CONFIG_PATH
        if settings.is_file():
            config = load_config(settings)
            zone, source = config.timezone, config.timezone_source
        else:
            zone, source = resolve_timezone({}, settings, user_dir() / PROFILE_NAME)
        written = render_launchd(
            _REPO_ROOT,
            Path.home(),
            user_dir(),
            args.out,
            label_prefix=args.label_prefix,
            vault_path=args.vault_path,
            timezone=zone,
        )
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"render-launchd: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Time zone: {zone} (from {source})")
    for path in written:
        print(path)
    print()
    print("Copy them to where launchd looks at every login, and load them from there:")
    print('  mkdir -p "$HOME/Library/LaunchAgents"')
    for path in written:
        print(f'  cp {shlex.quote(str(path))} "$HOME/Library/LaunchAgents/"')
    for path in written:
        print(f'  launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/{path.name}"')
    print("Each job starts when it is loaded. To load a job again, first unload it with")
    print("  launchctl bootout gui/$(id -u)/<label>")


def _run_init_vault(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli init-vault",
        description=(
            "Copy the starter vault into a folder. Existing files are never "
            "overwritten; folder names come from notes: in your profile.yaml."
        ),
    )
    parser.add_argument("path", type=Path, help="The vault folder; created if missing.")
    args = parser.parse_args(argv)

    from src.brain.connector.config import PROFILE_NAME, user_dir
    from src.brain.connector.layout import load_layout
    from src.brain.vault_init import init_vault

    try:
        layout = load_layout(user_dir() / PROFILE_NAME)
        result = init_vault(args.path, layout)
    except (ValueError, OSError) as exc:
        print(f"init-vault: {exc}", file=sys.stderr)
        sys.exit(1)
    if result.created:
        print(f"Created {len(result.created)} files:")
        for name in result.created:
            print(f"  {name}")
    if result.skipped:
        print(f"Skipped {len(result.skipped)} files that already exist:")
        for name in result.skipped:
            print(f"  {name}")
    if not result.created and not result.skipped:
        print("Nothing to do: the templates are empty.")


async def _run_orchestrated(mode: str):
    from src.orchestrator.runner import run_orchestrated
    await run_orchestrated(mode)


if __name__ == "__main__":
    main()
