from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["error", "warn", "info"]


@dataclass(frozen=True)
class Issue:
    file: str
    message: str
    line: int | None = None
    severity: Severity | None = None


@dataclass(frozen=True)
class Check:
    name: str
    severity: Severity
    fn: Callable
    connector: bool = False


CHECKS: list[Check] = []


def register(*, severity: Severity, connector: bool = False):
    """Register a check. ``connector`` marks the checks ``doctor --connector`` runs."""

    def decorator(fn):
        CHECKS.append(Check(name=fn.__name__, severity=severity, fn=fn, connector=connector))
        return fn

    return decorator


def issue_level(check: Check, issue: Issue) -> Severity:
    """An issue's own severity if it has one, else its check's."""
    return issue.severity or check.severity


def run_checks(
    repo_root: Path, *, strict: bool = False, connector_only: bool = False
) -> tuple[list[tuple[Check, list[Issue]]], int]:
    """Run all registered checks against the merged config.

    Returns `(results, exit_code)`.
    """
    from src.brain.doctor import checks  # noqa: F401
    from src.brain.runtime.loader import load_all

    cfg = load_all(repo_root)

    results: list[tuple[Check, list[Issue]]] = []
    error_count = 0

    if connector_only and not checks._connector_file().is_file():
        config_check = next(c for c in CHECKS if c.name == "check_connector_config")
        missing = Issue(
            file=str(checks._connector_file()),
            message=(
                "doctor --connector needs the connector settings; copy "
                f"{checks._CONNECTOR_EXAMPLE} to user/vault_connector.yaml and fill it in"
            ),
            severity="error",
        )
        return [(config_check, [missing])], 1

    for check in CHECKS:
        if connector_only and not check.connector:
            continue
        try:
            issues = check.fn(cfg, repo_root)
        except Exception as e:
            issues = [
                Issue(
                    file="<doctor>",
                    message=f"check {check.name} crashed: {type(e).__name__}: {e}",
                    severity="error",
                )
            ]
        results.append((check, issues))
        for issue in issues:
            level = issue_level(check, issue)
            if level != "info" and (level == "error" or strict):
                error_count += 1

    return results, (1 if error_count > 0 else 0)


def print_report(
    results: list[tuple[Check, list[Issue]]], *, strict: bool = False
) -> None:
    err_total = warn_total = 0
    for check, issues in results:
        if not issues:
            print(f"  ✓ {check.name}")
            continue
        levels = [issue_level(check, issue) for issue in issues]
        is_error = "error" in levels or (strict and any(lv != "info" for lv in levels))
        symbol = "✗" if is_error else "!" if "warn" in levels else "i"
        shown = "error" if "error" in levels else "warn" if "warn" in levels else "info"
        print(f"  {symbol} {check.name} ({shown})")
        for issue, level in zip(issues, levels, strict=True):
            loc = f"{issue.file}:{issue.line}" if issue.line else issue.file
            print(f"    - {loc}: {issue.message}")
            if level == "info":
                continue
            if level == "error" or strict:
                err_total += 1
            else:
                warn_total += 1
    print()
    if err_total:
        print(f"  FAIL — {err_total} error(s), {warn_total} warning(s)")
    elif warn_total:
        print(f"  PASS with {warn_total} warning(s)")
    else:
        print("  PASS — all checks green")
