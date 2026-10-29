import sqlite3

import pytest

from src.db import (
    _init_schema,
    content_hash,
    insert_agent_run,
    short_id,
    utcnow,
)


@pytest.fixture
def fresh_db():
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    yield conn
    conn.close()


class TestSchema:
    def test_all_tables_created(self, fresh_db):
        tables = {row[0] for row in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        expected = {
            "findings", "knowledge", "agent_runs", "notifications",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_findings_has_reasoning_column(self, fresh_db):
        columns = {row[1] for row in fresh_db.execute("PRAGMA table_info(findings)").fetchall()}
        assert "reasoning" in columns

    def test_insert_agent_run_persists_findings_count(self, fresh_db):
        insert_agent_run(
            fresh_db,
            agent="router",
            provider="pi",
            model="claude-haiku-5-5",
            findings_count=3,
        )
        row = fresh_db.execute(
            "SELECT findings_count FROM agent_runs WHERE agent='router'"
        ).fetchone()
        assert row == (3,)


class TestContentHash:
    def test_deterministic(self):
        h1 = content_hash("health:Pollen alert")
        h2 = content_hash("health:Pollen alert")
        assert h1 == h2

    def test_different_inputs(self):
        h1 = content_hash("health:Pollen alert")
        h2 = content_hash("career:New job posting")
        assert h1 != h2

class TestShortId:
    def test_uniqueness(self):
        ids = {short_id() for _ in range(1000)}
        assert len(ids) == 1000

class TestUtcnow:
    def test_format(self):
        now = utcnow()
        assert "T" in now
        assert len(now) >= 19


class TestFindingDedup:
    def test_content_hash_unique_constraint(self, fresh_db):
        h = content_hash("health:Pollen alert")
        now = utcnow()

        fresh_db.execute(
            "INSERT INTO findings (id,agent,category,title,detail,action,source_url,confidence,created_at,expires_at,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (short_id(), "health", "FYI", "Pollen alert", "", "", "", "MEDIUM", now, now, h)
        )

        cursor = fresh_db.execute(
            "INSERT OR IGNORE INTO findings (id,agent,category,title,detail,action,source_url,confidence,created_at,expires_at,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (short_id(), "health", "FYI", "Pollen alert", "", "", "", "MEDIUM", now, now, h)
        )
        assert cursor.rowcount == 0

    def test_different_hash_allows_insert(self, fresh_db):
        now = utcnow()

        fresh_db.execute(
            "INSERT INTO findings (id,agent,category,title,detail,action,source_url,confidence,created_at,expires_at,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (short_id(), "health", "FYI", "Pollen alert", "", "", "", "MEDIUM", now, now, content_hash("a"))
        )
        cursor = fresh_db.execute(
            "INSERT INTO findings (id,agent,category,title,detail,action,source_url,confidence,created_at,expires_at,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (short_id(), "health", "FYI", "Pollen alert 2", "", "", "", "MEDIUM", now, now, content_hash("b"))
        )
        assert cursor.rowcount == 1


class TestAgentRegistry:
    def test_private_channel_override_becomes_effective_registry_destination(self):
        from types import SimpleNamespace

        from src.brain.runtime.spec import AgentSpec, DeliverySpec, SlackDelivery
        from src.orchestrator.registry import _build_config

        cfg = SimpleNamespace(
            agents={
                "discovery": AgentSpec(
                    name="discovery",
                    kind="service",
                    delivery=DeliverySpec(
                        slack=SlackDelivery(channel_id="C0EXAMPLE04")
                    ),
                    mcp_tools=["mcp__brain-tools__push_notify"],
                ),
            },
            modes={},
            integrations={
                "notifier": {
                    "type": "slack",
                    "options": {
                        "channels": {
                            "discovery": {"channel_id": "CREALCHANNEL"},
                            "unknown": {"channel_id": "CIGNORED"},
                        },
                    },
                },
            },
        )

        agents, _, _ = _build_config(cfg)

        assert agents["discovery"]["delivery"]["slack"]["channel_id"] == (
            "CREALCHANNEL"
        )
        assert "unknown" not in agents

    def test_channel_override_cannot_grant_slack_capability(self):
        from types import SimpleNamespace

        from src.brain.runtime.spec import AgentSpec
        from src.orchestrator.registry import _build_config

        cfg = SimpleNamespace(
            agents={
                "system_agent": AgentSpec(
                    name="system_agent",
                    kind="system",
                    mcp_tools=["mcp__brain-tools__push_notify"],
                ),
            },
            modes={},
            integrations={
                "notifier": {
                    "type": "slack",
                    "options": {
                        "channels": {
                            "system_agent": {"channel_id": "CSHOULDNOTGRANT"},
                        },
                    },
                },
            },
        )

        agents, _, _ = _build_config(cfg)

        assert agents["system_agent"]["delivery"] is None
        assert "mcp__brain-tools__push_notify" not in agents["system_agent"]["tools"]

    def test_all_agents_have_required_fields(self):
        from src.orchestrator.registry import AGENT_REGISTRY
        for name, config in AGENT_REGISTRY.items():
            assert "model" in config, f"{name} missing 'model'"
            assert "tools" in config, f"{name} missing 'tools'"
            assert "max_turns" in config, f"{name} missing 'max_turns'"

    def test_run_modes_reference_valid_agents(self):
        from src.orchestrator.registry import AGENT_REGISTRY, RUN_MODES
        for mode_name, mode in RUN_MODES.items():
            all_agents = mode.get("pre", []) + mode.get("parallel", []) + mode.get("sequential", [])
            for agent in all_agents:
                assert agent in AGENT_REGISTRY, \
                    f"Mode '{mode_name}' references unknown agent '{agent}'"

    def test_agent_registry_populated(self):
        from src.orchestrator.registry import AGENT_REGISTRY
        assert len(AGENT_REGISTRY) >= 5
        for required in ("housekeeping", "alfred", "discovery"):
            assert required in AGENT_REGISTRY

    def test_morning_mode_runs_housekeeping_first_and_ends_with_the_closing_run(self):
        from src.orchestrator.registry import AGENT_REGISTRY, RUN_MODES

        morning = RUN_MODES["all_morning"]
        assert morning["pre"] == ["housekeeping"]
        assert AGENT_REGISTRY[morning["sequential"][-1]]["chat"] == "private"

    def test_the_shipped_morning_ends_with_the_closing_run_and_the_follow_up_run_is_at_night(self):
        from pathlib import Path

        import yaml

        repo = Path(__file__).resolve().parents[1]
        modes = yaml.safe_load((repo / "config" / "modes.yaml").read_text())["modes"]
        assert modes["all_morning"]["sequential"] == ["alfred_private"]
        assert modes["night"]["sequential"] == ["follow_up"]
