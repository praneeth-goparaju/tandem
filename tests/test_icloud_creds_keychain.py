from __future__ import annotations

import subprocess

import pytest

from src.brain.mcp.tools import _icloud

_REAL_KEYCHAIN_PASSWORD = _icloud._keychain_password


@pytest.fixture
def real_keychain(monkeypatch):
    monkeypatch.setattr(_icloud, "_keychain_password", _REAL_KEYCHAIN_PASSWORD)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(_icloud, "_keychain_passwords", {})
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "")
    monkeypatch.setattr(_icloud, "ICLOUD_APP_PASSWORD", "")


def _fake_run(calls: list, *, returncode: int = 0, stdout: str = "kc-secret\n"):
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    return run


def test_env_wins(monkeypatch, real_keychain):
    calls: list = []
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(_icloud, "ICLOUD_APP_PASSWORD", "env-secret")
    monkeypatch.setattr(_icloud.subprocess, "run", _fake_run(calls))

    assert _icloud.require_creds() == ("me@icloud.com", "env-secret")
    assert calls == []


def test_keychain_used_when_env_empty(monkeypatch, real_keychain):
    calls: list = []
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(_icloud.subprocess, "run", _fake_run(calls))

    assert _icloud.require_creds() == ("me@icloud.com", "kc-secret")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [
        "security",
        "find-generic-password",
        "-s",
        "brain-icloud-app-password",
        "-a",
        "me@icloud.com",
        "-w",
    ]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert 0 < kwargs["timeout"] <= 10


def test_neither_raises_with_both_names(monkeypatch, real_keychain):
    calls: list = []
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(
        _icloud.subprocess, "run", _fake_run(calls, returncode=44, stdout="")
    )

    with pytest.raises(_icloud.ICloudNotConfigured) as exc:
        _icloud.require_creds()

    message = str(exc.value)
    assert "ICLOUD_APPLE_ID" in message
    assert "ICLOUD_APP_PASSWORD" in message
    assert ".env" in message
    assert "brain-icloud-app-password" in message
    assert "security add-generic-password" in message


def test_missing_apple_id_raises_without_calling_security(monkeypatch, real_keychain):
    calls: list = []
    monkeypatch.setattr(_icloud.subprocess, "run", _fake_run(calls))

    with pytest.raises(_icloud.ICloudNotConfigured) as exc:
        _icloud.require_creds()

    assert "brain-icloud-app-password" in str(exc.value)
    assert calls == []


def test_nonzero_exit_ignores_output(monkeypatch, real_keychain):
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(
        _icloud.subprocess, "run", _fake_run([], returncode=44, stdout="leftover\n")
    )

    with pytest.raises(_icloud.ICloudNotConfigured):
        _icloud.require_creds()


def test_empty_output_counts_as_not_found(monkeypatch, real_keychain):
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(_icloud.subprocess, "run", _fake_run([], stdout="\n"))

    with pytest.raises(_icloud.ICloudNotConfigured):
        _icloud.require_creds()


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("security"), subprocess.TimeoutExpired("security", 5)],
)
def test_security_unavailable_or_slow_counts_as_not_found(monkeypatch, real_keychain, error):
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")

    def run(args, **kwargs):
        raise error

    monkeypatch.setattr(_icloud.subprocess, "run", run)

    with pytest.raises(_icloud.ICloudNotConfigured):
        _icloud.require_creds()


def test_keychain_result_is_cached(monkeypatch, real_keychain):
    calls: list = []
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")
    monkeypatch.setattr(_icloud.subprocess, "run", _fake_run(calls))

    first = _icloud.require_creds()
    second = _icloud.require_creds()

    assert first == second == ("me@icloud.com", "kc-secret")
    assert len(calls) == 1


def test_default_guard_never_runs_security(monkeypatch):
    # No real_keychain fixture here: this is what every other test sees.
    monkeypatch.setattr(_icloud, "ICLOUD_APPLE_ID", "me@icloud.com")

    def run(*args, **kwargs):
        raise AssertionError("a test ran the real security tool")

    monkeypatch.setattr(_icloud.subprocess, "run", run)

    with pytest.raises(_icloud.ICloudNotConfigured):
        _icloud.require_creds()
