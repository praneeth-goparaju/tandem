import importlib

import pytest


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    from src.orchestrator import config
    importlib.reload(config)


def test_memory_db_path_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.delenv("VAULT_DB_PATH", raising=False)
    from src.orchestrator import config
    importlib.reload(config)
    assert str(config.MEMORY_DB_PATH) == str(tmp_path / "m.db")


def test_vault_db_path_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)
    monkeypatch.setenv("MEMORY_DB_PATH", "")
    monkeypatch.setenv("VAULT_DB_PATH", str(tmp_path / "v.db"))
    from src.orchestrator import config
    importlib.reload(config)
    assert str(config.MEMORY_DB_PATH) == str(tmp_path / "v.db")


def test_runtime_paths_expand_home_consistently(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VAULT_PATH", "~/vault")
    monkeypatch.setenv("AGENT_NOTES_DIR", "~/agent-notes")
    monkeypatch.setenv("MEMORY_DB_PATH", "~/state/memory.db")
    monkeypatch.setenv("USER_DIR", "~/private-user")
    monkeypatch.setenv("OCCASIONS_DIR", "~/occasions")
    monkeypatch.setenv("GARMIN_TOKENSTORE", "~/garmin")

    from src.orchestrator import config

    importlib.reload(config)

    assert config.VAULT_PATH == tmp_path / "vault"
    assert config.AGENT_NOTES_DIR == tmp_path / "agent-notes"
    assert config.MEMORY_DB_PATH == tmp_path / "state" / "memory.db"
    assert config.USER_DIR == tmp_path / "private-user"
    assert config.OCCASIONS_DIR == tmp_path / "occasions"
    assert config.GARMIN_TOKENSTORE == tmp_path / "garmin"


def test_runtime_paths_are_absolute_even_when_env_is_relative(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VAULT_PATH", "relative-vault")
    monkeypatch.setenv("MEMORY_DB_PATH", "state/memory.db")

    from src.orchestrator import config

    importlib.reload(config)

    assert config.VAULT_PATH == tmp_path / "relative-vault"
    assert config.MEMORY_DB_PATH == tmp_path / "state" / "memory.db"


def test_runtime_paths_keep_symlinks_unresolved(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("VAULT_PATH", str(link))
    from src.orchestrator import config
    importlib.reload(config)
    assert config.VAULT_PATH == link
