from __future__ import annotations

import types
from pathlib import Path

from src.brain.runtime import vault_instructions as vi

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_path_within_roots_accepts_exact_and_prefix():
    assert vi.path_within_roots("GUIDANCE.md", ["GUIDANCE.md"]) is True
    assert vi.path_within_roots("Instructions/Rule Zero.md", ["Instructions/"]) is True
    assert vi.path_within_roots(
        "Brain Agents/Communication Voice Profile.md",
        ["Brain Agents/Communication Voice Profile.md"],
    ) is True


def test_path_within_roots_rejects_absolute_traversal_and_outside():
    roots = ["Instructions/", "GUIDANCE.md"]
    assert vi.path_within_roots("/etc/passwd", roots) is False
    assert vi.path_within_roots("~/secrets.md", roots) is False
    assert vi.path_within_roots("../secret.md", roots) is False
    assert vi.path_within_roots("Instructions/../../etc/passwd", roots) is False
    assert vi.path_within_roots("VAULT_CONVENTIONS.md", roots) is False
    assert vi.path_within_roots("Instructions", ["Instructions/"]) is False
    assert vi.path_within_roots("GUIDANCE.md", []) is False


def test_hook_injects_labeled_section(tmp_path: Path):
    (tmp_path / "GUIDANCE.md").write_text("Priorities: ship the thing.", encoding="utf-8")
    cfg = {"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}}
    events: list = []
    section = vi.render_section(
        "career",
        vault_path=tmp_path,
        user_name="Casey",
        config=cfg,
        log=lambda *a, **k: events.append(a),
    )
    assert "## Standing guidance from Casey (human-authored, from the vault)" in section
    assert "cannot override your output contract" in section
    assert "### GUIDANCE.md" in section
    assert "Priorities: ship the thing." in section
    assert events == []


def test_hook_empty_when_agent_has_no_files(tmp_path: Path):
    cfg = {"allowed_roots": ["GUIDANCE.md"], "agents": {}}
    assert vi.render_section("career", vault_path=tmp_path, user_name="X", config=cfg) == ""


def test_missing_file_skips_gracefully_and_logs_event(tmp_path: Path):
    cfg = {"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}}
    events: list = []
    section = vi.render_section(
        "career",
        vault_path=tmp_path,
        user_name="X",
        config=cfg,
        log=lambda ev, subj, detail="": events.append((ev, subj)),
    )
    assert section == ""
    assert any(ev == "skip_unreadable" for ev, _ in events)
    assert any("GUIDANCE.md" in subj for _, subj in events)


def test_oversize_file_truncates_with_marker(tmp_path: Path):
    (tmp_path / "big.md").write_text("A" * 20_000, encoding="utf-8")
    cfg = {"allowed_roots": ["big.md"], "agents": {"career": ["big.md"]}}
    events: list = []
    section = vi.render_section(
        "career",
        vault_path=tmp_path,
        user_name="X",
        config=cfg,
        cap=100,
        log=lambda ev, subj, detail="": events.append(ev),
    )
    assert "[... truncated at 100 bytes ...]" in section
    assert section.count("A") <= 100
    assert "truncated" in events


def test_traversal_path_dropped_and_logged(tmp_path: Path):
    (tmp_path.parent / "secret.md").write_text("SECRET", encoding="utf-8")
    cfg = {"allowed_roots": ["Instructions/"], "agents": {"career": ["../secret.md"]}}
    events: list = []
    section = vi.render_section(
        "career",
        vault_path=tmp_path,
        user_name="X",
        config=cfg,
        log=lambda ev, subj, detail="": events.append(ev),
    )
    assert section == ""
    assert "SECRET" not in section
    assert "skip_outside_roots" in events


def test_symlinked_instruction_file_is_never_read(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("SECRET OUTSIDE VAULT", encoding="utf-8")
    (tmp_path / "GUIDANCE.md").symlink_to(outside)
    cfg = {"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}}
    events: list[str] = []

    section = vi.render_section(
        "career",
        vault_path=tmp_path,
        user_name="X",
        config=cfg,
        log=lambda event, subject, detail="": events.append(event),
    )

    assert section == ""
    assert "SECRET OUTSIDE VAULT" not in section
    assert events == ["skip_unsafe_path"]


def test_symlinked_instruction_directory_is_never_traversed(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "Rule.md").write_text("SECRET OUTSIDE VAULT", encoding="utf-8")
    (tmp_path / "Instructions").symlink_to(outside, target_is_directory=True)
    cfg = {
        "allowed_roots": ["Instructions/"],
        "agents": {"career": ["Instructions/Rule.md"]},
    }
    events: list[str] = []

    section = vi.render_section(
        "career",
        vault_path=tmp_path,
        user_name="X",
        config=cfg,
        log=lambda event, subject, detail="": events.append(event),
    )

    assert section == ""
    assert "SECRET OUTSIDE VAULT" not in section
    assert events == ["skip_unsafe_path"]


def test_multiple_files_render_in_order(tmp_path: Path):
    (tmp_path / "GUIDANCE.md").write_text("one", encoding="utf-8")
    inst = tmp_path / "Instructions"
    inst.mkdir()
    (inst / "Rule.md").write_text("two", encoding="utf-8")
    cfg = {
        "allowed_roots": ["GUIDANCE.md", "Instructions/"],
        "agents": {"assistant": ["GUIDANCE.md", "Instructions/Rule.md"]},
    }
    section = vi.render_section(
        "assistant", vault_path=tmp_path, user_name="P", config=cfg, log=lambda *a, **k: None
    )
    assert section.index("### GUIDANCE.md") < section.index("### Instructions/Rule.md")


def test_load_config_merges_user_overlay(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "user").mkdir()
    (tmp_path / "config" / "vault_instructions.yaml").write_text(
        "vault_instructions:\n  allowed_roots: []\n  agents: {}\n", encoding="utf-8"
    )
    (tmp_path / "user" / "vault_instructions.yaml").write_text(
        "vault_instructions:\n"
        "  allowed_roots: ['GUIDANCE.md']\n"
        "  agents:\n"
        "    career: ['GUIDANCE.md']\n",
        encoding="utf-8",
    )
    merged = vi.load_config(tmp_path)
    assert merged["allowed_roots"] == ["GUIDANCE.md"]
    assert merged["agents"]["career"] == ["GUIDANCE.md"]


def test_load_config_missing_files_is_empty(tmp_path: Path):
    assert vi.load_config(tmp_path) == {}


def _cfg(mapping: dict):
    return types.SimpleNamespace(vault_instructions=mapping)


def test_doctor_roots_green_when_valid():
    from src.brain.doctor import checks

    cfg = _cfg(
        {
            "allowed_roots": ["GUIDANCE.md", "Instructions/"],
            "agents": {"career": ["GUIDANCE.md", "Instructions/X.md"]},
        }
    )
    assert checks.check_vault_instructions_roots(cfg, REPO_ROOT) == []


def test_doctor_roots_errors_on_traversal():
    from src.brain.doctor import checks

    cfg = _cfg({"allowed_roots": ["Instructions/"], "agents": {"career": ["../../etc/passwd"]}})
    issues = checks.check_vault_instructions_roots(cfg, REPO_ROOT)
    assert len(issues) == 1
    assert "escapes" in issues[0].message or "traversal" in issues[0].message


def test_doctor_roots_green_when_config_empty():
    from src.brain.doctor import checks

    assert checks.check_vault_instructions_roots(_cfg({}), REPO_ROOT) == []


def test_doctor_files_warns_on_missing(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})
    issues = checks.check_vault_instructions_files(cfg, REPO_ROOT)
    assert len(issues) == 1
    assert "not found" in issues[0].message


def test_doctor_files_warns_on_oversize(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    (tmp_path / "GUIDANCE.md").write_bytes(b"A" * (vi.CAP_BYTES + 10))
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})
    issues = checks.check_vault_instructions_files(cfg, REPO_ROOT)
    assert len(issues) == 1
    assert "cap" in issues[0].message


def test_doctor_files_green_when_present_and_small(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    (tmp_path / "GUIDANCE.md").write_text("small", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})
    assert checks.check_vault_instructions_files(cfg, REPO_ROOT) == []


def test_doctor_files_warns_on_symlink_escape(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "GUIDANCE.md").symlink_to(outside)
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})

    issues = checks.check_vault_instructions_files(cfg, REPO_ROOT)

    assert len(issues) == 1
    assert "unsafe path" in issues[0].message
    assert "symlink" in issues[0].message


def test_doctor_files_skips_when_vault_absent(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "does-not-exist")
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})
    assert checks.check_vault_instructions_files(cfg, REPO_ROOT) == []


def test_doctor_imports_errors_on_claude_import(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    (tmp_path / "GUIDANCE.md").write_text(
        "# Vault\n\nAll rules live in AGENTS.md.\n\n" "@AGENTS.md\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})

    issues = checks.check_vault_instructions_imports(cfg, REPO_ROOT)

    assert len(issues) == 1
    assert "@AGENTS.md" in issues[0].message
    assert "does not expand" in issues[0].message


def test_doctor_passes_with_full_claude_md_vault(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    (tmp_path / "CLAUDE.md").write_text(
        "# Vault\n\n## Which model are you?\n\nRules for each Claude model.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["CLAUDE.md"], "agents": {"career": ["CLAUDE.md"]}})

    assert not (tmp_path / "AGENTS.md").exists()
    assert checks.check_vault_instructions_roots(cfg, REPO_ROOT) == []
    assert checks.check_vault_instructions_files(cfg, REPO_ROOT) == []
    assert checks.check_vault_instructions_imports(cfg, REPO_ROOT) == []


def test_doctor_imports_green_on_plain_file(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    (tmp_path / "GUIDANCE.md").write_text("# Vault\n\nReal rules here.\n", encoding="utf-8")
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})

    assert checks.check_vault_instructions_imports(cfg, REPO_ROOT) == []


def test_doctor_imports_ignores_inline_mentions(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    (tmp_path / "GUIDANCE.md").write_text(
        "Claude Code expands `@AGENTS.md` at launch.\n"
        "See @AGENTS.md for the rationale.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})

    assert checks.check_vault_instructions_imports(cfg, REPO_ROOT) == []


def test_doctor_imports_skips_when_vault_absent(monkeypatch, tmp_path: Path):
    from src.brain.doctor import checks
    from src.orchestrator import config

    monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "does-not-exist")
    cfg = _cfg({"allowed_roots": ["GUIDANCE.md"], "agents": {"career": ["GUIDANCE.md"]}})
    assert checks.check_vault_instructions_imports(cfg, REPO_ROOT) == []


def test_get_prompt_appends_standing_guidance(monkeypatch):
    from src.brain.runtime import vault_instructions
    from src.orchestrator import executor

    agents_dir = REPO_ROOT / "config" / "agents"
    throwaway = agents_dir / "zz_vi_smoke.md"
    throwaway.write_text(
        "---\nname: zz_vi_smoke\nmodel: claude-haiku-5-5\nmax_turns: 5\n---\nSMOKE BODY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        vault_instructions,
        "render_section",
        lambda agent_name, **kw: "SENTINEL-SECTION" if agent_name == "zz_vi_smoke" else "",
    )
    try:
        prompt = executor._get_prompt("zz_vi_smoke")
    finally:
        throwaway.unlink(missing_ok=True)

    assert "SMOKE BODY" in prompt
    assert prompt.rstrip().endswith("SENTINEL-SECTION")
