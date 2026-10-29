from __future__ import annotations

import asyncio

from src.brain.actions.handlers.edit_vault_files import MAX_CHANGES, execute


def _run(payload):
    from src.brain.actions.types import validate_vault_note_path
    from src.brain.atomic_io import confined_path, regular_file_sha256
    from src.orchestrator.config import VAULT_PATH

    for change in payload.get("changes") or []:
        if not isinstance(change, dict) or "expected_sha256" in change:
            continue
        try:
            target = confined_path(VAULT_PATH, validate_vault_note_path(change.get("path")))
        except ValueError:
            continue
        change["expected_sha256"] = regular_file_sha256(target)
    return asyncio.run(execute(payload))


def _propose(payload: dict) -> dict:
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.actions import propose_action

    with caller_scope(ToolCaller(
        agent="consistency",
        area="consistency",
        allowed_action_types=frozenset({"edit_vault_file", "edit_vault_files"}),
    )):
        return asyncio.run(propose_action(payload))


def _list_pending() -> dict:
    from src.brain.mcp.caller_context import ToolCaller, caller_scope
    from src.brain.mcp.tools.actions import list_pending_actions

    with caller_scope(ToolCaller(agent="consistency", area="consistency")):
        return asyncio.run(list_pending_actions({"status": "pending"}))


def _tmp_litter(vault):
    return [p for p in vault.rglob("*.tmp")]


def test_happy_path_applies_all_and_logs_event(isolated_db, isolated_vault):
    a = isolated_vault / "Areas" / "a.md"
    a.parent.mkdir(parents=True)
    a.write_text("# A\n\nold a\n", encoding="utf-8")
    b = isolated_vault / "b.md"
    b.write_text("# B\n\nold b\n", encoding="utf-8")

    result = _run({
        "summary": "renamed the widget to gadget",
        "changes": [
            {"path": "Areas/a.md", "new_content": "# A\n\nnew a: gadget\n"},
            {"path": "b.md", "new_content": "# B\n\nnew b: gadget\n"},
            {"path": "c.md", "new_content": "# C\n\nbrand new: gadget\n"},
        ],
        "_action_id": "act1",
    })
    assert result["ok"] is True

    assert a.read_text(encoding="utf-8") == "# A\n\nnew a: gadget\n"
    assert b.read_text(encoding="utf-8") == "# B\n\nnew b: gadget\n"
    assert (isolated_vault / "c.md").read_text(encoding="utf-8") == "# C\n\nbrand new: gadget\n"
    assert _tmp_litter(isolated_vault) == []

    conn = isolated_db.get_db()
    actor, action, subject, trust, detail = conn.execute(
        "SELECT actor, action, subject, trust, detail FROM events WHERE action='vault_files_edited'"
    ).fetchone()
    assert action == "vault_files_edited"
    assert subject == "renamed the widget to gadget"
    assert trust == "trusted"
    assert "Areas/a.md" in detail and "b.md" in detail and "c.md" in detail
    assert "action act1" in detail
    assert conn.execute(
        "SELECT count(*) FROM events WHERE action='vault_files_edited'"
    ).fetchone()[0] == 1


def test_batch_refuses_hidden_vault_configuration(isolated_vault):
    result = _run({
        "changes": [{
            "path": ".obsidian/plugins/agent/config.md",
            "new_content": "enabled: true\n",
        }],
    })
    assert result["ok"] is False
    assert "unsafe vault note path" in result["result"]


def test_idempotent_noop_writes_nothing_and_no_event(isolated_db, isolated_vault):
    f = isolated_vault / "same.md"
    f.write_text("# same\n", encoding="utf-8")
    mtime_before = f.stat().st_mtime_ns

    result = _run({"summary": "no change", "changes": [{"path": "same.md", "new_content": "# same\n"}]})
    assert result["ok"] is True
    assert "unchanged" in result["result"].lower()
    assert f.stat().st_mtime_ns == mtime_before

    conn = isolated_db.get_db()
    assert conn.execute(
        "SELECT count(*) FROM events WHERE action='vault_files_edited'"
    ).fetchone()[0] == 0


def test_cap_rejection(isolated_vault):
    changes = [{"path": f"f{i}.md", "new_content": "x"} for i in range(MAX_CHANGES + 1)]
    result = _run({"summary": "too big", "changes": changes})
    assert result["ok"] is False
    assert "too many changes" in result["result"]
    assert "split by topic" in result["result"]
    assert list(isolated_vault.glob("f*.md")) == []


def test_at_cap_is_allowed(isolated_db, isolated_vault):
    changes = [{"path": f"f{i}.md", "new_content": f"content {i}\n"} for i in range(MAX_CHANGES)]
    result = _run({"summary": "exactly at the cap", "changes": changes})
    assert result["ok"] is True
    assert len(list(isolated_vault.glob("f*.md"))) == MAX_CHANGES


def test_missing_changes_rejected(isolated_vault):
    assert _run({"summary": "x"})["ok"] is False
    assert _run({"summary": "x", "changes": []})["ok"] is False
    assert _run({"summary": "x", "changes": "notalist"})["ok"] is False


def test_duplicate_path_rejected(isolated_vault):
    result = _run({
        "summary": "dup",
        "changes": [
            {"path": "dup.md", "new_content": "one"},
            {"path": "dup.md", "new_content": "two"},
        ],
    })
    assert result["ok"] is False
    assert "duplicate path" in result["result"]
    assert not (isolated_vault / "dup.md").exists()


def test_parent_traversal_is_rejected_even_when_it_normalizes_inside(isolated_vault):
    result = _run({
        "summary": "dup",
        "changes": [
            {"path": "sub/x.md", "new_content": "one"},
            {"path": "sub/../sub/x.md", "new_content": "two"},
        ],
    })
    assert result["ok"] is False
    assert "unsafe vault note path" in result["result"]


def test_traversal_rejected_and_nothing_written(isolated_vault):
    result = _run({
        "summary": "escape attempt",
        "changes": [
            {"path": "safe.md", "new_content": "safe"},
            {"path": "../escape.md", "new_content": "evil"},
        ],
    })
    assert result["ok"] is False
    assert "unsafe vault note path" in result["result"]
    assert not (isolated_vault / "safe.md").exists()
    assert not (isolated_vault.parent / "escape.md").exists()


def test_protected_block_preserved_in_batch(isolated_db, isolated_vault):
    daily = isolated_vault / "daily.md"
    daily.write_text(
        "# Day\n\n## Auto\n\nold auto\n\n## Quick Capture\n\nuser typed this\n",
        encoding="utf-8",
    )
    other = isolated_vault / "other.md"
    other.write_text("# Other\n\nold\n", encoding="utf-8")

    result = _run({
        "summary": "refresh auto sections",
        "changes": [
            {
                "path": "daily.md",
                "new_content": "# Day\n\n## Auto\n\nfresh auto\n",
                "preserve_sections": ["Quick Capture"],
            },
            {"path": "other.md", "new_content": "# Other\n\nfresh\n"},
        ],
    })
    assert result["ok"] is True
    text = daily.read_text(encoding="utf-8")
    assert "fresh auto" in text
    assert "old auto" not in text
    assert "## Quick Capture" in text
    assert "user typed this" in text
    assert other.read_text(encoding="utf-8") == "# Other\n\nfresh\n"


def test_validation_failure_file_3_of_4_changes_nothing(isolated_db, isolated_vault):
    for name in ("v1.md", "v2.md", "v4.md"):
        (isolated_vault / name).write_text(f"orig {name}\n", encoding="utf-8")

    result = _run({
        "summary": "cascade with one bad file",
        "changes": [
            {"path": "v1.md", "new_content": "new v1\n"},
            {"path": "v2.md", "new_content": "new v2\n"},
            {"path": "v3.md"},
            {"path": "v4.md", "new_content": "new v4\n"},
        ],
    })
    assert result["ok"] is False
    assert "missing new_content" in result["result"]

    assert (isolated_vault / "v1.md").read_text(encoding="utf-8") == "orig v1.md\n"
    assert (isolated_vault / "v2.md").read_text(encoding="utf-8") == "orig v2.md\n"
    assert (isolated_vault / "v4.md").read_text(encoding="utf-8") == "orig v4.md\n"
    assert not (isolated_vault / "v3.md").exists()
    assert _tmp_litter(isolated_vault) == []

    conn = isolated_db.get_db()
    assert conn.execute(
        "SELECT count(*) FROM events WHERE action='vault_files_edited'"
    ).fetchone()[0] == 0


def test_stale_later_change_prevents_every_batch_write(isolated_vault):
    first = isolated_vault / "first.md"
    second = isolated_vault / "second.md"
    first.write_text("first old\n", encoding="utf-8")
    second.write_text("second changed after approval\n", encoding="utf-8")

    result = _run({
        "summary": "stale batch",
        "changes": [
            {"path": "first.md", "new_content": "first new\n"},
            {
                "path": "second.md",
                "new_content": "second new\n",
                "expected_sha256": "0" * 64,
            },
        ],
    })

    assert result["ok"] is False and result["retryable"] is True
    assert "changed after approval" in result["result"]
    assert first.read_text(encoding="utf-8") == "first old\n"
    assert second.read_text(encoding="utf-8") == "second changed after approval\n"
    assert _tmp_litter(isolated_vault) == []


def test_staging_failure_cleans_temps_and_is_retryable(isolated_db, isolated_vault, monkeypatch):
    (isolated_vault / "s1.md").write_text("orig s1\n", encoding="utf-8")
    (isolated_vault / "s2.md").write_text("orig s2\n", encoding="utf-8")

    payload = {
        "summary": "staging boom",
        "changes": [
            {"path": "s1.md", "new_content": "new s1\n"},
            {"path": "s2.md", "new_content": "new s2\n"},
            {"path": "s3.md", "new_content": "new s3\n"},
        ],
    }

    from src.brain.actions.handlers import edit_vault_files as module

    real_stage_text = module.stage_text
    calls = {"n": 0}

    def flaky_stage_text(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_stage_text(*a, **k)

    monkeypatch.setattr(module, "stage_text", flaky_stage_text)
    result = _run(payload)
    assert result["ok"] is False
    assert result["retryable"] is True
    assert "staging failed" in result["result"]

    assert (isolated_vault / "s1.md").read_text(encoding="utf-8") == "orig s1\n"
    assert (isolated_vault / "s2.md").read_text(encoding="utf-8") == "orig s2\n"
    assert not (isolated_vault / "s3.md").exists()
    assert _tmp_litter(isolated_vault) == []

    monkeypatch.setattr(module, "stage_text", real_stage_text)
    retry = _run(payload)
    assert retry["ok"] is True
    assert (isolated_vault / "s1.md").read_text(encoding="utf-8") == "new s1\n"
    assert (isolated_vault / "s2.md").read_text(encoding="utf-8") == "new s2\n"
    assert (isolated_vault / "s3.md").read_text(encoding="utf-8") == "new s3\n"


def test_write_failure_after_mkstemp_cleans_its_own_temp(isolated_db, isolated_vault, monkeypatch):
    import os as _os

    (isolated_vault / "w1.md").write_text("orig w1\n", encoding="utf-8")
    (isolated_vault / "w2.md").write_text("orig w2\n", encoding="utf-8")

    payload = {
        "summary": "write boom",
        "changes": [
            {"path": "w1.md", "new_content": "new w1\n"},
            {"path": "w2.md", "new_content": "new w2\n"},
        ],
    }

    real_fdopen = _os.fdopen
    calls = {"writes": 0}

    def flaky_fdopen(fd, *a, **k):
        mode = str(a[0] if a else k.get("mode", "r"))
        if "w" in mode:
            calls["writes"] += 1
        if "w" in mode and calls["writes"] == 2:
            _os.close(fd)
            raise OSError(28, "No space left on device")
        return real_fdopen(fd, *a, **k)

    monkeypatch.setattr(_os, "fdopen", flaky_fdopen)
    result = _run(payload)
    assert result["ok"] is False
    assert result["retryable"] is True
    assert "staging failed" in result["result"]

    assert _tmp_litter(isolated_vault) == []
    assert (isolated_vault / "w1.md").read_text(encoding="utf-8") == "orig w1\n"
    assert (isolated_vault / "w2.md").read_text(encoding="utf-8") == "orig w2\n"

    monkeypatch.setattr(_os, "fdopen", real_fdopen)
    retry = _run(payload)
    assert retry["ok"] is True
    assert (isolated_vault / "w2.md").read_text(encoding="utf-8") == "new w2\n"


def test_case_variant_duplicate_paths_rejected(isolated_vault):
    """APFS is case-insensitive: Foo.md and foo.md are the same file, so a
    payload naming both is an internally-contradictory cascade -> rejected."""
    (isolated_vault / "Topic.md").write_text("orig\n", encoding="utf-8")
    result = _run({
        "summary": "case dup",
        "changes": [
            {"path": "Topic.md", "new_content": "a\n"},
            {"path": "topic.md", "new_content": "b\n"},
        ],
    })
    assert result["ok"] is False
    assert "duplicate path" in result["result"]
    assert (isolated_vault / "Topic.md").read_text(encoding="utf-8") == "orig\n"


def test_partial_replace_failure_is_logged_loudly(isolated_db, isolated_vault, monkeypatch):
    (isolated_vault / "p1.md").write_text("orig p1\n", encoding="utf-8")
    (isolated_vault / "p2.md").write_text("orig p2\n", encoding="utf-8")

    from src.brain.actions.handlers import edit_vault_files as module

    real_commit = module.commit_staged_if_sha256
    calls = {"n": 0}

    def flaky_commit(src, dst, expected):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("rename exploded")
        return real_commit(src, dst, expected)

    monkeypatch.setattr(module, "commit_staged_if_sha256", flaky_commit)
    result = _run({
        "summary": "partial apply",
        "changes": [
            {"path": "p1.md", "new_content": "new p1\n"},
            {"path": "p2.md", "new_content": "new p2\n"},
        ],
    })
    assert result["ok"] is False
    assert "partial cascade" in result["result"].lower()

    assert (isolated_vault / "p1.md").read_text(encoding="utf-8") == "new p1\n"
    assert (isolated_vault / "p2.md").read_text(encoding="utf-8") == "orig p2\n"
    assert _tmp_litter(isolated_vault) == []

    conn = isolated_db.get_db()
    actor, subject, detail = conn.execute(
        "SELECT actor, subject, detail FROM events WHERE action='vault_cascade_partial'"
    ).fetchone()
    assert "p1.md" in detail
    assert conn.execute(
        "SELECT count(*) FROM events WHERE action='vault_files_edited'"
    ).fetchone()[0] == 0


def test_pending_rendering_shows_summary_and_per_file(isolated_db, isolated_vault):
    out = _propose({
        "type": "edit_vault_files",
        "payload": {
            "summary": "renamed widget to gadget everywhere",
            "changes": [
                {"path": "Areas/a.md", "new_content": "the gadget spec, first line\n\nbody\n"},
                {"path": "b.md", "new_content": "second file body\n"},
            ],
        },
        "rationale": "cascade the rename across both files that state it",
    })
    assert "queued" in out["content"][0]["text"]

    listed = _list_pending()
    text = listed["content"][0]["text"]
    assert "renamed widget to gadget everywhere" in text
    assert "Areas/a.md" in text
    assert "b.md" in text
    assert "bytes" in text
    assert "the gadget spec, first line" in text


def test_risk_auto_is_clamped_to_confirm_and_pends(isolated_db, isolated_vault):
    from src.brain.actions import queue

    (isolated_vault / "z.md").write_text("orig\n", encoding="utf-8")
    out = _propose({
        "type": "edit_vault_files",
        "payload": {"summary": "sneaky auto", "changes": [{"path": "z.md", "new_content": "new\n"}]},
        "rationale": "agent tried to self-approve a cascade",
        "risk": "auto",
    })
    assert "queued" in out["content"][0]["text"]
    pending = queue.list_by_status("pending")
    assert len(pending) == 1 and pending[0]["risk"] == "confirm"
    assert (isolated_vault / "z.md").read_text(encoding="utf-8") == "orig\n"


def test_confirm_then_human_approval_applies_via_executor(isolated_db, isolated_vault):
    from src.brain.actions import queue
    from src.brain.actions.approvals import handle_reply
    from src.brain.actions.executor import drain_once

    (isolated_vault / "e1.md").write_text("orig 1\n", encoding="utf-8")
    (isolated_vault / "e2.md").write_text("orig 2\n", encoding="utf-8")

    _propose({
        "type": "edit_vault_files",
        "payload": {
            "summary": "cascade the fact",
            "changes": [
                {"path": "e1.md", "new_content": "fixed 1\n"},
                {"path": "e2.md", "new_content": "fixed 2\n"},
            ],
        },
        "rationale": "two files state the same fact; update both",
        "risk": "confirm",
    })
    action_id = queue.list_by_status("pending")[0]["id"]
    row = queue.get(action_id)
    queue.record_review_delivery(
        action_id, row["payload_hash"], "consistency", "CCONSISTENCY", "1.1"
    )
    assert (isolated_vault / "e1.md").read_text(encoding="utf-8") == "orig 1\n"

    assert handle_reply(
        f"yes {action_id}", area="consistency", actor="UOWNER", owner=True
    ) is True
    assert asyncio.run(drain_once()) == 1

    assert queue.get(action_id)["status"] == "executed"
    assert (isolated_vault / "e1.md").read_text(encoding="utf-8") == "fixed 1\n"
    assert (isolated_vault / "e2.md").read_text(encoding="utf-8") == "fixed 2\n"


def test_pending_rendering_single_edit_vault_file(isolated_db, isolated_vault):
    _propose({
        "type": "edit_vault_file",
        "payload": {"path": "solo.md", "new_content": "# Solo\n\njust one file\n"},
        "rationale": "one-file edit",
        "risk": "confirm",
    })
    listed = _list_pending()
    text = listed["content"][0]["text"]
    assert "solo.md" in text
    assert "bytes" in text
    assert "# Solo" in text
