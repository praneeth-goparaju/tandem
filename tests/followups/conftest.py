import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PROJECTS = Path(__file__).parent.parent / "fixtures" / "followups" / "projects"
RECENT = datetime(2026, 10, 14, 12, tzinfo=UTC)
FIXTURE_MTIMES = {
    "-Users-me-brain/s1.jsonl": RECENT,
    "-Users-me-brain/broken.jsonl": RECENT + timedelta(hours=1),
    "-Users-me-client-work/s2.jsonl": RECENT + timedelta(hours=2),
    "-Users-me-old/s3.jsonl": datetime(2026, 9, 1, tzinfo=UTC),
}


@pytest.fixture(scope="session", autouse=True)
def _transcript_fixture_mtimes():
    for name, moment in FIXTURE_MTIMES.items():
        stamp = moment.timestamp()
        os.utime(PROJECTS / name, (stamp, stamp))
