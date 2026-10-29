import asyncio
import json
import logging
import os
import shutil
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

from src.brain.followups import collect as collector
from src.brain.followups.collect import collect, load_config
from src.brain.followups.digest import load_state, promote_cursor
from src.db import list_follow_ups, upsert_follow_up

pytestmark = pytest.mark.usefixtures("isolated_db")

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_USER = REPO_ROOT / "tests" / "fixtures" / "user-ci"
PROJECTS = REPO_ROOT / "tests" / "fixtures" / "followups" / "projects"
HOME = Path("/home/user")
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
INTEGRATIONS = """\
notifier:
  type: slack
  options:
    channels:
      alfred: {channel_id: C0SHARED1}
      alfred_private: {channel_id: D0PRIVATE1}
    authorized_senders:
      U0COOWNER: {name: Sam, areas: [alfred]}
"""


@pytest.fixture(autouse=True)
def _no_owner_id(monkeypatch):
    monkeypatch.delenv("OWNER_SLACK_USER_ID", raising=False)


def slack_ts(moment: datetime) -> str:
    return f"{moment.timestamp():.6f}"


def post(moment: datetime, text: str, user: str = "U1") -> dict:
    return {"type": "message", "user": user, "text": text, "ts": slack_ts(moment)}


class FakeSlack:
    def __init__(
        self,
        history: dict[str, list[dict]],
        broken: tuple[str, ...] = (),
        replies: dict[str, list[dict]] | None = None,
        limited: tuple[str, ...] = (),
    ):
        self.history = history
        self.broken = broken
        self.replies = replies or {}
        self.limited = set(limited)
        self.calls: list[tuple[str, str]] = []
        self.thread_reads: list[str] = []

    async def conversations_history(self, *, channel, oldest, limit, cursor=None, **params):
        self.calls.append((channel, oldest))
        if channel in self.broken:
            raise RuntimeError("channel_not_found")
        return {
            "ok": True,
            "messages": self.history.get(channel, []),
            "response_metadata": {"next_cursor": ""},
        }

    async def auth_test(self):
        return {"ok": True, "bot_id": "B0ALFRED", "user_id": "U0ALFRED"}

    async def conversations_replies(self, *, channel, ts, oldest, limit, cursor=None, **params):
        self.thread_reads.append(ts)
        if ts in self.limited:
            self.limited.discard(ts)
            response = SimpleNamespace(status_code=429, get=lambda key, default=None: default)
            raise SlackApiError("ratelimited", response)
        return {"ok": True, "messages": self.replies[ts], "response_metadata": {"next_cursor": ""}}


def threads(count: int, start: datetime) -> FakeSlack:
    parents, replies = [], {}
    for number in range(count):
        moment = start + timedelta(minutes=number)
        answered = moment + timedelta(seconds=30)
        parent = {
            **post(moment, f"question {number}"),
            "reply_count": 1,
            "latest_reply": slack_ts(answered),
            "thread_ts": slack_ts(moment),
        }
        answer = {**post(answered, f"answer {number}", user="U2"), "thread_ts": parent["ts"]}
        parents.insert(0, parent)
        replies[parent["ts"]] = [parent, answer]
    return FakeSlack({"C0SHARED1": parents}, replies=replies)


def make_user(base: Path, *, slack: bool = True, followups: str | None = None) -> Path:
    user_dir = base / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    if slack:
        (user_dir / "integrations.yaml").write_text(INTEGRATIONS)
    if followups is not None:
        (user_dir / "followups.yaml").write_text(followups)
    return user_dir


def run(
    user_dir: Path,
    *,
    now: datetime = NOW,
    client=None,
    factory=None,
    projects: Path = PROJECTS,
    clock=None,
) -> dict:
    if factory is None and client is not None:

        def factory():
            return client

    return asyncio.run(
        collect(
            user_dir=user_dir,
            repo_root=REPO_ROOT,
            projects_dir=projects,
            home=HOME,
            now=now,
            client_factory=factory,
            **({"clock": clock} if clock else {}),
        )
    )


def digest_text(user_dir: Path) -> str:
    return sorted((user_dir / "followups").glob("digest-*.md"))[-1].read_text()


def state(user_dir: Path) -> dict:
    return load_state(user_dir / "followups")


def cursor(moment: datetime) -> dict:
    return {"oldest": slack_ts(moment)}


def test_the_first_run_reads_fourteen_days_back_and_returns_the_counts(tmp_path):
    user_dir = make_user(tmp_path)
    client = FakeSlack(
        {
            "C0SHARED1": [
                post(NOW - timedelta(days=15), "too old to read"),
                post(NOW - timedelta(days=13), "old enough to read"),
            ],
            "D0PRIVATE1": [post(NOW - timedelta(hours=1), "this morning", user="U2")],
        }
    )
    counts = run(user_dir, client=client)
    digest = digest_text(user_dir)
    assert "old enough to read" in digest and "this morning" in digest
    assert "too old to read" not in digest
    morning = slack_ts(NOW - timedelta(hours=1)).replace(".", "")
    assert f"· user:U2 · https://slack.com/archives/D0PRIVATE1/p{morning}\n" in digest
    assert "Can you check the Garmin sync?" in digest
    assert counts == {
        "slack": 2,
        "claude_code": 5,
        "bytes": len(digest.encode()),
        "expired": 0,
        "slack_errors": 0,
        "slack_unfinished": 0,
    }
    assert ("C0SHARED1", slack_ts(NOW - timedelta(days=28))) in client.calls
    assert state(user_dir) == {
        "committed": {},
        "pending": {
            "slack": {"C0SHARED1": cursor(NOW), "D0PRIVATE1": cursor(NOW)},
            "transcripts": NOW.isoformat(),
        },
    }


def test_the_collector_writes_todays_index_of_every_ref_beside_the_digest(tmp_path):
    user_dir = make_user(tmp_path)
    shared = post(NOW - timedelta(hours=2), "who books the dentist?")
    private = post(NOW - timedelta(hours=1), "is the chart ready?")
    run(user_dir, client=FakeSlack({"C0SHARED1": [shared], "D0PRIVATE1": [private]}))
    path = user_dir / "followups" / "index-2026-10-16.json"
    refs = json.loads(path.read_text())
    shared_ref = f"https://slack.com/archives/C0SHARED1/p{shared['ts'].replace('.', '')}"
    private_ref = f"https://slack.com/archives/D0PRIVATE1/p{private['ts'].replace('.', '')}"
    assert refs[shared_ref] == {"source": "slack_shared", "place": "shared"}
    assert refs[private_ref] == {"source": "slack_private", "place": "private"}
    assert refs["-Users-me-brain/s1.jsonl:1"] == {"source": "claude_code", "place": "private"}
    assert len(refs) == 7
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_once_a_session_read_the_digest_the_next_run_starts_after_it(tmp_path):
    user_dir = make_user(tmp_path)
    yesterday = {"C0SHARED1": [post(NOW - timedelta(days=1), "yesterday")]}
    run(user_dir, client=FakeSlack(yesterday))
    assert promote_cursor(user_dir) is True
    later = NOW + timedelta(hours=1)
    again = FakeSlack(yesterday)
    counts = run(user_dir, now=later, client=again)
    assert (counts["slack"], counts["claude_code"]) == (0, 0)
    assert again.calls[0] == ("C0SHARED1", slack_ts(NOW - timedelta(days=14)))
    assert digest_text(user_dir) == ""
    assert state(user_dir)["committed"]["slack"]["C0SHARED1"] == cursor(NOW)
    assert state(user_dir)["pending"]["slack"]["C0SHARED1"] == cursor(later)


@pytest.mark.parametrize("later", [timedelta(hours=1), timedelta(days=1)], ids=["same day", "next day"])
def test_a_window_no_session_read_is_collected_again(tmp_path, later):
    user_dir = make_user(tmp_path)
    shared = {"C0SHARED1": [post(NOW - timedelta(hours=1), "who books the dentist?")]}
    run(user_dir, client=FakeSlack(shared))
    counts = run(user_dir, now=NOW + later, client=FakeSlack(shared))
    digest = digest_text(user_dir)
    assert "who books the dentist?" in digest and "Can you check the Garmin sync?" in digest
    assert (counts["slack"], counts["claude_code"]) == (1, 5)
    assert state(user_dir)["committed"] == {}


def test_what_arrives_while_the_collector_runs_waits_for_the_next_digest(tmp_path):
    projects = tmp_path / "projects"
    transcript = projects / "-Users-me-brain" / "s.jsonl"
    transcript.parent.mkdir(parents=True)
    lines = [
        {
            "type": "user",
            "message": {"role": "user", "content": f"typed {label}"},
            "timestamp": moment.isoformat().replace("+00:00", "Z"),
            "cwd": "/home/user/w",
            "isSidechain": False,
            "entrypoint": "cli",
        }
        for label, moment in (
            ("before the run", NOW - timedelta(hours=1)),
            ("during the run", NOW + timedelta(minutes=1)),
        )
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    stamp = (NOW + timedelta(minutes=1)).timestamp()
    os.utime(transcript, (stamp, stamp))
    shared = {
        "C0SHARED1": [
            post(NOW + timedelta(minutes=1), "posted during the run"),
            post(NOW - timedelta(minutes=5), "posted before the run"),
        ]
    }
    user_dir = make_user(tmp_path)
    run(user_dir, client=FakeSlack(shared), projects=projects)
    first = digest_text(user_dir)
    assert "typed before the run" in first and "posted before the run" in first
    assert "during the run" not in first
    promote_cursor(user_dir)
    run(user_dir, now=NOW + timedelta(hours=1), client=FakeSlack(shared), projects=projects)
    second = digest_text(user_dir)
    assert "typed during the run" in second and "posted during the run" in second
    assert "before the run" not in second


def test_the_owner_the_co_owner_and_our_bot_are_named_and_others_by_their_id(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OWNER_SLACK_USER_ID", "U0OWNER")
    shared = [
        post(NOW - timedelta(minutes=4), "from the owner", user="U0OWNER"),
        post(NOW - timedelta(minutes=3), "from the co-owner", user="U0COOWNER"),
        post(NOW - timedelta(minutes=2), "from a guest", user="U0GUEST"),
        {**post(NOW - timedelta(minutes=1), "from Alfred", user="U0ALFRED"), "bot_id": "B0ALFRED"},
        {**post(NOW - timedelta(seconds=30), "from an app", user=None), "bot_id": "B0OTHER"},
    ]
    user_dir = make_user(tmp_path)
    run(user_dir, client=FakeSlack({"C0SHARED1": shared}))
    whos = [line.split(" · ")[2] for line in digest_text(user_dir).splitlines() if line.startswith("### slack")]
    assert whos == ["bot:B0OTHER", "alfred", "user:U0GUEST", "co-owner", "owner"]


def test_excluded_projects_from_followups_yaml_are_not_read(tmp_path):
    user_dir = make_user(tmp_path, slack=False)
    shutil.copy(CI_USER / "followups.yaml", user_dir / "followups.yaml")
    counts = run(user_dir)
    digest = digest_text(user_dir)
    assert counts["claude_code"] == 4
    assert "secret client question" not in digest
    assert "Can you check the Garmin sync?" in digest


def test_the_digest_holds_the_scrubbed_line_and_no_raw_secret(tmp_path):
    secret = "sk-" + "proj" + "A1b2" * 12
    user_dir = make_user(tmp_path)
    client = FakeSlack(
        {"D0PRIVATE1": [post(NOW - timedelta(hours=1), f"my key is {secret}, please rotate it")]}
    )
    run(user_dir, client=client)
    digest = digest_text(user_dir)
    assert "my key is [redacted], please rotate it" in digest
    assert secret not in digest and "A1b2" not in digest


def test_old_follow_ups_are_expired_and_counted(tmp_path):
    for key, seen in (("k-old", "2026-09-20T05:00:00"), ("k-new", "2026-10-15T05:00:00")):
        upsert_follow_up(
            source="slack_shared",
            ref=key,
            asked_by=None,
            place="shared",
            summary=key,
            dedupe_key=key,
            first_seen=seen,
        )
    counts = run(make_user(tmp_path, slack=False))
    assert counts["expired"] == 1
    assert [row["dedupe_key"] for row in list_follow_ups(status="expired")] == ["k-old"]


@pytest.mark.parametrize("why", ["slack off", "no token"])
def test_a_run_that_did_not_read_slack_leaves_the_slack_cursor_where_it_was(
    tmp_path, monkeypatch, why
):
    def refuse():
        raise AssertionError("a Slack client was built")

    if why == "slack off":
        monkeypatch.setenv("BRAIN_SLACK_OFF", "1")
    user_dir = make_user(tmp_path)
    counts = run(user_dir, factory=refuse if why == "slack off" else None)
    assert (counts["slack"], counts["slack_errors"], counts["claude_code"]) == (0, 0, 5)
    assert state(user_dir)["pending"]["slack"] == {}
    promote_cursor(user_dir)
    monkeypatch.delenv("BRAIN_SLACK_OFF", raising=False)
    shared = {"C0SHARED1": [post(NOW - timedelta(hours=1), "sent before Slack was read")]}
    run(user_dir, now=NOW + timedelta(days=1), client=FakeSlack(shared))
    assert "sent before Slack was read" in digest_text(user_dir)


def test_a_failing_channel_is_logged_and_counted_and_the_others_are_kept(tmp_path, caplog):
    user_dir = make_user(tmp_path)
    client = FakeSlack(
        {"C0SHARED1": [post(NOW - timedelta(hours=2), "from the shared channel")]},
        broken=("D0PRIVATE1",),
    )
    with caplog.at_level(logging.ERROR):
        counts = run(user_dir, client=client)
    assert (counts["slack"], counts["slack_errors"]) == (1, 1)
    assert "from the shared channel" in digest_text(user_dir)
    assert "D0PRIVATE1" in caplog.text


def test_a_channel_that_failed_is_read_again_from_where_it_left_off(tmp_path):
    user_dir = make_user(tmp_path)
    run(user_dir, client=FakeSlack({}, broken=("D0PRIVATE1",)))
    assert state(user_dir)["pending"]["slack"] == {
        "C0SHARED1": cursor(NOW),
        "D0PRIVATE1": cursor(NOW - timedelta(days=14)),
    }
    promote_cursor(user_dir)
    client = FakeSlack({"D0PRIVATE1": [post(NOW - timedelta(days=2), "sent while it was failing")]})
    counts = run(user_dir, now=NOW + timedelta(days=1), client=client)
    assert (counts["slack"], counts["slack_errors"]) == (1, 0)
    assert "sent while it was failing" in digest_text(user_dir)
    assert ("D0PRIVATE1", slack_ts(NOW - timedelta(days=28))) in client.calls
    assert ("C0SHARED1", slack_ts(NOW - timedelta(days=14))) in client.calls
    assert state(user_dir)["pending"]["slack"]["D0PRIVATE1"] == cursor(NOW + timedelta(days=1))


def test_at_most_40_threads_are_read_a_run_and_the_next_run_goes_on_from_there(tmp_path):
    user_dir = make_user(tmp_path)
    client = threads(60, NOW - timedelta(days=3))
    first = run(user_dir, client=client)
    assert (first["slack_unfinished"], first["slack_errors"]) == (1, 0)
    assert len(client.thread_reads) == 40
    digest = digest_text(user_dir)
    assert "answer 39" in digest and "question 40" not in digest
    promote_cursor(user_dir)
    client.thread_reads.clear()
    second = run(user_dir, now=NOW + timedelta(hours=1), client=client)
    assert (second["slack_unfinished"], len(client.thread_reads)) == (0, 20)
    digest = digest_text(user_dir)
    assert "question 40" in digest and "answer 59" in digest and "answer 39" not in digest
    assert state(user_dir)["pending"]["slack"]["C0SHARED1"] == cursor(NOW + timedelta(hours=1))


def test_a_rate_limited_thread_stops_the_run_and_the_next_one_resumes_there(tmp_path):
    user_dir = make_user(tmp_path)
    client = threads(5, NOW - timedelta(days=1))
    client.limited.add(slack_ts(NOW - timedelta(days=1) + timedelta(minutes=2)))
    first = run(user_dir, client=client)
    assert (first["slack_errors"], first["slack_unfinished"]) == (0, 2)
    assert "answer 1" in digest_text(user_dir) and "question 2" not in digest_text(user_dir)
    promote_cursor(user_dir)
    second = run(user_dir, now=NOW + timedelta(days=1), client=client)
    assert (second["slack_errors"], second["slack_unfinished"]) == (0, 0)
    digest = digest_text(user_dir)
    assert "answer 2" in digest and "answer 4" in digest and "answer 1" not in digest


def test_slack_stops_after_90_seconds_and_the_digest_keeps_what_was_read(tmp_path):
    ticks = iter(range(0, 3000, 20))
    user_dir = make_user(tmp_path)
    counts = run(user_dir, client=threads(5, NOW - timedelta(days=1)), clock=lambda: next(ticks))
    assert (counts["claude_code"], counts["slack"], counts["slack_unfinished"]) == (5, 4, 2)
    digest = digest_text(user_dir)
    assert "answer 1" in digest and "question 2" not in digest
    assert "Can you check the Garmin sync?" in digest
    pending = state(user_dir)["pending"]["slack"]
    assert pending["D0PRIVATE1"] == cursor(NOW - timedelta(days=14))
    assert pending["C0SHARED1"]["until"] == slack_ts(NOW)


def test_old_channels_are_read_through_their_date_and_not_after(tmp_path):
    cfg = 'old_channels: ["C0OLDFIN1"]\nold_channels_until: 2026-10-16\n'
    old = {"C0OLDFIN1": [post(NOW - timedelta(hours=2), "from the old finance channel")]}
    on_the_day = FakeSlack(old)
    run(make_user(tmp_path / "day", followups=cfg), client=on_the_day)
    assert "C0OLDFIN1" in [channel for channel, _ in on_the_day.calls]
    after = FakeSlack(old)
    user_dir = make_user(tmp_path / "after", followups=cfg)
    counts = run(user_dir, now=NOW + timedelta(days=1), client=after)
    assert "C0OLDFIN1" not in [channel for channel, _ in after.calls]
    assert counts["slack"] == 0


def test_load_config_lays_the_user_file_over_the_shipped_defaults(tmp_path):
    assert load_config(tmp_path, REPO_ROOT) == {
        "exclude_projects": [],
        "exclude_paths": [],
    }
    assert load_config(CI_USER, REPO_ROOT) == {
        "exclude_projects": ["-Users-me-client-work"],
        "exclude_paths": [],
    }
    (tmp_path / "followups.yaml").write_text("old_channels_until: 2026-11-15\n")
    assert load_config(tmp_path, REPO_ROOT)["old_channels_until"] == date(2026, 11, 15)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("exclude_projects", '"-Users-me-client-work"'),
        ("exclude_projects", "[1, 2]"),
        ("exclude_paths", '"acme"'),
        ("exclude_paths", "[3]"),
        ("exclude_paths", '["work/acme"]'),
        ("exclude_paths", '[""]'),
    ],
)
def test_load_config_refuses_exclusions_that_are_not_a_list_of_folder_names(tmp_path, key, value):
    (tmp_path / "followups.yaml").write_text(f"{key}: {value}\n")
    with pytest.raises(ValueError, match=key):
        load_config(tmp_path, REPO_ROOT)


def test_excluded_paths_from_followups_yaml_are_not_read(tmp_path):
    user_dir = make_user(tmp_path, slack=False, followups='exclude_paths: ["client-work"]\n')
    counts = run(user_dir)
    assert counts["claude_code"] == 4
    assert "secret client question" not in digest_text(user_dir)


def main_world(tmp_path, monkeypatch, env_file: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / ".env").write_text(env_file)
    (repo / "config" / "integrations.yaml").write_text(INTEGRATIONS)
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-Users-me-brain"
    project.mkdir(parents=True)
    line = {
        "type": "user",
        "message": {"role": "user", "content": "Is the weight chart ready?"},
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "cwd": "/home/user/w",
        "isSidechain": False,
        "entrypoint": "cli",
    }
    (project / "s.jsonl").write_text(json.dumps(line) + "\n")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    monkeypatch.setattr(collector, "REPO_ROOT", repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER_DIR", str(user_dir))
    for key in ("SLACK_BOT_TOKEN", "BRAIN_SLACK_OFF", "OWNER_SLACK_USER_ID"):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    return user_dir


def test_main_takes_the_token_from_the_env_file_runs_and_records_the_event(
    tmp_path, monkeypatch, capsys, isolated_db
):
    main_world(tmp_path, monkeypatch, "SLACK_BOT_TOKEN=xoxb-from-file\n")
    tokens = []

    handlers = []

    class Client(FakeSlack):
        def __init__(self, token, retry_handlers):
            tokens.append(token)
            handlers.extend(retry_handlers)
            soon = datetime.now(UTC) - timedelta(minutes=5)
            super().__init__({"C0SHARED1": [post(soon, "from slack")]})

    monkeypatch.setattr(collector, "AsyncWebClient", Client)
    assert collector.main() == 0
    counts = json.loads(capsys.readouterr().out)
    assert tokens == ["xoxb-from-file"]
    assert [type(handler) for handler in handlers] == [AsyncRateLimitErrorRetryHandler]
    assert handlers[0].max_retry_count == 1
    assert (counts["slack"], counts["claude_code"], counts["slack_errors"]) == (1, 1, 0)
    rows = isolated_db.get_db().execute("SELECT actor, action, detail FROM events").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("followups:collector", "followups_collected")]
    assert json.loads(rows[0][2]) == counts


def test_main_takes_the_owner_id_from_the_env_file(tmp_path, monkeypatch, capsys):
    user_dir = main_world(tmp_path, monkeypatch, "SLACK_BOT_TOKEN=xoxb-from-file\nOWNER_SLACK_USER_ID=U0OWNER\n")

    class Client(FakeSlack):
        def __init__(self, token, retry_handlers):
            soon = datetime.now(UTC) - timedelta(minutes=5)
            super().__init__({"C0SHARED1": [post(soon, "from the owner", user="U0OWNER")]})

    monkeypatch.setattr(collector, "AsyncWebClient", Client)
    assert collector.main() == 0
    digest = next((user_dir / "followups").glob("digest-*.md")).read_text()
    assert "· owner · https://slack.com/archives/C0SHARED1/" in digest


def test_main_prefers_a_variable_already_in_the_environment_over_the_env_file(
    tmp_path, monkeypatch, capsys
):
    main_world(tmp_path, monkeypatch, "SLACK_BOT_TOKEN=xoxb-from-file\n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    tokens = []

    class Client(FakeSlack):
        def __init__(self, token, retry_handlers):
            tokens.append(token)
            super().__init__({})

    monkeypatch.setattr(collector, "AsyncWebClient", Client)
    assert collector.main() == 0
    assert tokens == ["xoxb-from-env"]


def test_main_with_slack_off_in_the_env_file_builds_no_client(tmp_path, monkeypatch, capsys):
    main_world(tmp_path, monkeypatch, "BRAIN_SLACK_OFF=1\nSLACK_BOT_TOKEN=xoxb-from-file\n")

    def refuse(**_):
        raise AssertionError("a Slack client was built")

    monkeypatch.setattr(collector, "AsyncWebClient", refuse)
    assert collector.main() == 0
    counts = json.loads(capsys.readouterr().out)
    assert (counts["slack"], counts["claude_code"]) == (0, 1)


def test_main_logs_a_failure_and_returns_1_without_recording_an_event(
    tmp_path, monkeypatch, caplog, isolated_db
):
    main_world(tmp_path, monkeypatch, "")

    async def broken(**_):
        raise RuntimeError("disk full")

    monkeypatch.setattr(collector, "collect", broken)
    with caplog.at_level(logging.ERROR):
        assert collector.main() == 1
    assert "disk full" in caplog.text
    assert isolated_db.get_db().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
