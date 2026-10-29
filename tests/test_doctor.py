from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.brain.doctor import CHECKS, Issue, run_checks
from src.brain.doctor import checks as _checks_module  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_USER = REPO_ROOT / "tests" / "fixtures" / "user-ci"


@pytest.fixture(autouse=True)
def _no_live_mail_or_icloud(monkeypatch):
    for name in ("ICLOUD_APPLE_ID", "ICLOUD_APP_PASSWORD", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        monkeypatch.setenv(name, "")


def _seed_synthetic_user(repo_root: Path) -> None:
    import os
    import shutil

    target = repo_root / "user"
    shutil.copytree(CI_USER, target)
    for current, _dirs, files in os.walk(target):
        Path(current).chmod(0o700)
        for name in files:
            (Path(current) / name).chmod(0o600)


def test_checks_registered() -> None:
    names = {c.name for c in CHECKS}
    expected = {
        "check_jinja_includes",
        "check_modes_resolve",
        "check_chain_targets",
        "check_dead_code",
    }
    missing = expected - names
    assert not missing, f"missing checks: {missing}"


def test_doctor_passes_against_real_repo(monkeypatch) -> None:
    monkeypatch.setenv("USER_DIR", str(CI_USER))
    monkeypatch.setenv("BRAIN_DOCTOR_SKIP_RUNTIME", "1")
    results, exit_code = run_checks(REPO_ROOT, strict=False)
    errors_by_check = {
        check.name: issues
        for check, issues in results
        if check.severity == "error" and issues
    }
    assert exit_code == 0, (
        f"doctor reported errors: "
        f"{ {n: [str(i) for i in iss] for n, iss in errors_by_check.items()} }"
    )
    issues_by_check = {check.name: issues for check, issues in results}
    for name in ("check_channel_format", "check_vault_paths_folder_slash"):
        assert issues_by_check[name] == [], f"{name}: {[str(i) for i in issues_by_check[name]]}"


def test_doctor_rejects_unknown_and_duplicate_action_capabilities() -> None:
    from src.brain.runtime.spec import AgentSpec

    cfg = SimpleNamespace(agents={
        "bad": AgentSpec(
            name="bad",
            allowed_action_types=["not_real", "not_real"],
        ),
    })

    issues = _checks_module.check_agent_tool_allowlists(cfg, REPO_ROOT)
    messages = {issue.message for issue in issues}
    assert "allowed_action_types contains unknown type 'not_real'" in messages
    assert "allowed_action_types contains duplicate entries" in messages


def test_doctor_requires_action_capability_for_wrapper_tool() -> None:
    from src.brain.runtime.spec import AgentSpec

    cfg = SimpleNamespace(agents={
        "bad": AgentSpec(
            name="bad",
            mcp_tools=["mcp__brain-tools__push_notify"],
            allowed_action_types=[],
        ),
    })

    issues = _checks_module.check_agent_tool_allowlists(cfg, REPO_ROOT)
    assert any(
        "tool 'push_notify' requires allowed action type 'push_notification'"
        in issue.message
        for issue in issues
    )


def test_doctor_rejects_unbounded_generic_action_tool() -> None:
    from src.brain.runtime.spec import AgentSpec

    cfg = SimpleNamespace(agents={
        "bad": AgentSpec(
            name="bad",
            mcp_tools=["mcp__brain-tools__propose_action"],
            allowed_action_types=[],
        ),
    })

    issues = _checks_module.check_agent_tool_allowlists(cfg, REPO_ROOT)
    assert any(
        "propose_action requires an explicit non-empty allowed_action_types"
        in issue.message
        for issue in issues
    )


def test_doctor_validates_scheduler_and_integration_config(monkeypatch) -> None:
    from src.brain.runtime.spec import ModeSpec

    cfg = SimpleNamespace(
        agents={},
        modes={"bad": ModeSpec(name="bad", schedule="not a cron")},
        integrations={"notifier": "unknown", "tasks": {"type": "todoist"}},
    )
    monkeypatch.setenv("TIMEZONE", "Not/A_Real_Zone")
    monkeypatch.setenv("BRAIN_REPLY_TIMEOUT", "forever")
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)

    scheduler_issues = _checks_module.check_scheduler_configuration(cfg, REPO_ROOT)
    integration_issues = _checks_module.check_integration_configuration(cfg, REPO_ROOT)

    assert any("invalid TIMEZONE" in issue.message for issue in scheduler_issues)
    assert any("BRAIN_REPLY_TIMEOUT" in issue.message for issue in scheduler_issues)
    assert any("unknown notifier backend" in issue.message for issue in integration_issues)
    assert any("TODOIST_API_TOKEN" in issue.message for issue in integration_issues)


def test_doctor_accepts_private_slack_channel_override(monkeypatch) -> None:
    agent = SimpleNamespace(
        delivery=SimpleNamespace(
            slack=SimpleNamespace(channel_id="C0EXAMPLE04"),
        ),
    )
    cfg = SimpleNamespace(
        agents={"discovery": agent},
        integrations={
            "notifier": {
                "type": "slack",
                "options": {
                    "channels": {"discovery": {"channel_id": "C0123456789"}},
                },
            },
            "tasks": "noop",
        },
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "test-app-token")
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")

    issues = _checks_module.check_integration_configuration(cfg, REPO_ROOT)

    assert not any("example Slack channel_id" in issue.message for issue in issues)


def test_doctor_rejects_unknown_slack_channel_binding(monkeypatch) -> None:
    def _agent(channel_id: str):
        return SimpleNamespace(
            delivery=SimpleNamespace(
                slack=SimpleNamespace(channel_id=channel_id),
            ),
        )

    cfg = SimpleNamespace(
        agents={
            "discovery": _agent("CDISCOVERY"),
        },
        integrations={
            "notifier": {
                "type": "slack",
                "options": {
                    "channels": {
                        "discovery": {"channel_id": "CDISCOVERY"},
                        "ghost": {"channel_id": "CGHOST"},
                    },
                },
            },
            "tasks": "noop",
        },
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "test-app-token")
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "UOWNER")

    issues = _checks_module.check_integration_configuration(cfg, REPO_ROOT)

    assert any("does not name a registered agent" in issue.message for issue in issues)


def test_doctor_enforces_private_overlay_without_reading_it(
    monkeypatch, tmp_path: Path
) -> None:
    user_dir = tmp_path / "private-user"
    user_dir.mkdir(mode=0o755)
    secret = user_dir / "profile.yaml"
    secret.write_text("do-not-read")
    secret.chmod(0o644)
    monkeypatch.setenv("USER_DIR", str(user_dir))

    issues = _checks_module.check_private_configuration_permissions(
        SimpleNamespace(), tmp_path
    )

    assert any("0700/file 0600" in issue.message for issue in issues)


def test_claude_runtime_check_passes_with_pinned_sdk(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    assert _checks_module.check_claude_runtime(SimpleNamespace(), tmp_path) == []


def test_claude_runtime_check_reports_wrong_sdk_version(monkeypatch, tmp_path: Path) -> None:
    import claude_agent_sdk

    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    monkeypatch.setattr(claude_agent_sdk, "__version__", "0.0.1")
    issues = _checks_module.check_claude_runtime(SimpleNamespace(), tmp_path)
    assert any("0.2.160" in issue.message for issue in issues)


def test_claude_runtime_check_skipped_in_ci(monkeypatch, tmp_path: Path) -> None:
    import claude_agent_sdk

    monkeypatch.setenv("BRAIN_DOCTOR_SKIP_RUNTIME", "1")
    monkeypatch.setattr(claude_agent_sdk, "__version__", "0.0.1")
    assert _checks_module.check_claude_runtime(SimpleNamespace(), tmp_path) == []


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_claude_auth_rejects_api_key(monkeypatch, tmp_path: Path, name: str) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert _checks_module.check_claude_auth(SimpleNamespace(), tmp_path) == []
    monkeypatch.setenv(name, "sk-test")
    issues = _checks_module.check_claude_auth(SimpleNamespace(), tmp_path)
    assert any(name in issue.message for issue in issues)


def test_tool_allowlist_accepts_the_grantable_builtins_and_still_rejects_the_rest(
    tmp_path: Path,
) -> None:
    from src.brain.runtime.spec import AgentSpec

    granted = AgentSpec(
        name="worker", builtins=["Read", "Glob", "Grep", "Edit", "Write", "Agent"], max_helpers=2,
    )
    cfg = SimpleNamespace(agents={"worker": granted})
    assert _checks_module.check_agent_tool_allowlists(cfg, tmp_path) == []

    for denied in ("Bash", "MultiEdit", "NotebookEdit", "WebSearch", "WebFetch", "Skill", "Task"):
        spec = AgentSpec.model_construct(name="shell", builtins=[denied])
        issues = _checks_module.check_agent_tool_allowlists(
            SimpleNamespace(agents={"shell": spec}), tmp_path,
        )
        assert any(
            denied in issue.message and "Claude runtime" in issue.message for issue in issues
        ), denied


def test_vault_runtime_path_requires_existing_real_directory(
    monkeypatch, tmp_path: Path
) -> None:
    from src.brain.doctor import checks
    from src.orchestrator import config

    monkeypatch.delenv("BRAIN_DOCTOR_SKIP_RUNTIME", raising=False)
    missing = tmp_path / "missing-vault"
    monkeypatch.setattr(config, "VAULT_PATH", missing)
    issues = checks.check_vault_runtime_path(SimpleNamespace(), tmp_path)
    assert len(issues) == 1
    assert "does not exist" in issues[0].message

    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", vault)
    assert checks.check_vault_runtime_path(SimpleNamespace(), tmp_path) == []

    link = tmp_path / "vault-link"
    link.symlink_to(vault, target_is_directory=True)
    monkeypatch.setattr(config, "VAULT_PATH", link)
    issues = checks.check_vault_runtime_path(SimpleNamespace(), tmp_path)
    assert len(issues) == 1
    assert "real directory" in issues[0].message

    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    issues = checks.check_vault_runtime_path(SimpleNamespace(), tmp_path / "brain")
    assert len(issues) == 1
    assert "application directory" in issues[0].message


def test_vault_runtime_path_skips_only_under_explicit_runtime_skip(
    monkeypatch, tmp_path: Path
) -> None:
    from src.brain.doctor import checks
    from src.orchestrator import config

    monkeypatch.setenv("BRAIN_DOCTOR_SKIP_RUNTIME", "1")
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path / "missing-vault")
    assert checks.check_vault_runtime_path(SimpleNamespace(), tmp_path) == []


def test_planted_unknown_agent_in_mode_fires_check_modes_resolve(
    tmp_path: Path, monkeypatch
) -> None:
    import shutil

    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    _seed_synthetic_user(tmp_path)
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))

    user_modes = tmp_path / "user" / "modes.yaml"
    user_modes.parent.mkdir(parents=True, exist_ok=True)
    text = user_modes.read_text() if user_modes.exists() else "modes:\n"
    text += "\n  bad_smoke_mode:\n    parallel: [definitely_not_an_agent]\n"
    user_modes.write_text(text)

    results, exit_code = run_checks(tmp_path, strict=False)
    issues_by_check = {check.name: issues for check, issues in results}
    assert exit_code == 1, "expected doctor to fail"
    assert any(
        "definitely_not_an_agent" in i.message
        for i in issues_by_check.get("check_modes_resolve", [])
    ), "check_modes_resolve should flag the planted unknown agent"


def test_planted_dead_symbol_warns(tmp_path: Path, monkeypatch) -> None:
    import shutil

    for sub in ("config", "src"):
        shutil.copytree(REPO_ROOT / sub, tmp_path / sub)
    _seed_synthetic_user(tmp_path)
    monkeypatch.setenv("USER_DIR", str(tmp_path / "user"))

    bad_file = tmp_path / "src" / "_doctor_test_planted.py"
    bad_file.write_text("# regression: this should trigger check_dead_code\nwrite_daily_note = None\n")
    monkeypatch.setenv("BRAIN_DOCTOR_SKIP_RUNTIME", "1")

    try:
        results, exit_code = run_checks(tmp_path, strict=False)
        warnings = next(
            (issues for check, issues in results if check.name == "check_dead_code"),
            [],
        )
        assert any(
            "write_daily_note" in i.message and i.file.endswith("_doctor_test_planted.py")
            for i in warnings
        ), f"check_dead_code should flag planted symbol; got {warnings}"
        assert exit_code == 0
        results_strict, exit_strict = run_checks(tmp_path, strict=True)
        assert exit_strict == 1
    finally:
        bad_file.unlink(missing_ok=True)
