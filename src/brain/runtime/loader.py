from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.brain.atomic_io import read_regular_text
from src.brain.runtime.spec import AgentSpec, ModeSpec
from src.brain.runtime.vault_instructions import load_config as load_vault_instructions

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class LoadedConfig:
    repo_root: Path
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    modes: dict[str, ModeSpec] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    integrations: dict[str, Any] = field(default_factory=dict)
    vault_instructions: dict[str, Any] = field(default_factory=dict)


def parse_agent_markdown(path: Path) -> AgentSpec:
    text = read_regular_text(path)
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path}: missing YAML frontmatter between --- fences")
    fm_text, body = m.group(1), m.group(2)
    frontmatter = yaml.safe_load(fm_text) or {}
    frontmatter.setdefault("name", path.stem)
    frontmatter["body"] = body
    return AgentSpec(**frontmatter)


def load_agents(config_dir: Path, user_dir: Path | None = None) -> dict[str, AgentSpec]:
    """Load all agents from config/agents/*.md plus user/agents/*.md overrides.

    Files starting with underscore are treated as shared fragments (includes),
    not agents — they're skipped here.
    """
    agents_dir = config_dir / "agents"
    out: dict[str, AgentSpec] = {}
    for md in sorted(agents_dir.glob("*.md")):
        if md.stem.startswith("_"):
            continue
        spec = parse_agent_markdown(md)
        out[spec.name] = spec
    if user_dir is not None:
        user_agents = user_dir / "agents"
        if user_agents.is_dir():
            for md in sorted(user_agents.glob("*.md")):
                if md.stem.startswith("_"):
                    continue
                spec = parse_agent_markdown(md)
                out[spec.name] = spec
    return out


def load_modes(config_dir: Path, user_dir: Path | None = None) -> dict[str, ModeSpec]:
    """Load orchestration modes from config/modes.yaml, overlaid by user/modes.yaml.

    user/modes.yaml entries OVERRIDE config/modes.yaml entries with the same
    name. This lets a forked deployment customize cron schedules or add new
    modes without editing the repo-shipped defaults.
    """
    def _load(path: Path) -> dict:
        try:
            text = read_regular_text(path)
        except FileNotFoundError:
            return {}
        return (yaml.safe_load(text) or {}).get("modes", {}) or {}

    merged = {}
    merged.update(_load(config_dir / "modes.yaml"))
    if user_dir is not None:
        merged.update(_load(user_dir / "modes.yaml"))

    return {name: ModeSpec(name=name, **body) for name, body in merged.items()}


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = read_regular_text(path)
    except FileNotFoundError:
        return {}
    return yaml.safe_load(text) or {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def resolve_user_dir(repo_root: Path) -> Path:
    return Path(
        os.path.abspath(
            Path(os.getenv("USER_DIR", str(repo_root / "user"))).expanduser()
        )
    )


def load_integrations(config_dir: Path, user_dir: Path | None = None) -> dict[str, Any]:
    base = load_yaml(config_dir / "integrations.yaml")
    if user_dir is None:
        return base
    overlay = load_yaml(user_dir / "integrations.yaml")
    return _deep_merge(base, overlay) if overlay else base


def load_all(repo_root: Path) -> LoadedConfig:
    config_dir = repo_root / "config"
    user_dir = resolve_user_dir(repo_root)
    cfg = LoadedConfig(repo_root=repo_root)
    cfg.agents = load_agents(config_dir, user_dir)
    cfg.modes = load_modes(config_dir, user_dir)
    cfg.profile = load_yaml(user_dir / "profile.yaml")
    cfg.sources = load_yaml(user_dir / "sources.yaml")
    cfg.integrations = load_integrations(config_dir, user_dir)
    cfg.vault_instructions = load_vault_instructions(repo_root, user_dir=user_dir)
    return cfg
