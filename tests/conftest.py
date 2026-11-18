import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sprinto_agent import clock, db  # noqa: E402
from sprinto_agent.config import Mode, Settings  # noqa: E402
from sprinto_agent.context import build_context  # noqa: E402

NOW = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)  # Tue 11:30 Asia/Kolkata
DEMO = "demo.inbox@example.com"


@pytest.fixture(autouse=True)
def fixed_clock():
    clock.set_now(NOW)
    yield
    clock.set_now(None)


@pytest.fixture
def settings(tmp_path):
    return Settings(mode=Mode.SIMULATION, db_path=str(tmp_path / "t.db"),
                    checkpoint_db_path=str(tmp_path / "cp.db"), demo_recipient_email=DEMO,
                    retry_backoff_override=[0, 0], sender_display_name="Demo Sender")


@pytest.fixture
def ctx(settings):
    c = build_context(settings)
    c.sleep = lambda s: None
    yield c
    if "checkpoint_conn" in c.extras:
        c.extras["checkpoint_conn"].close()
    c.conn.close()


def new_ctx(settings):
    c = build_context(settings)
    c.sleep = lambda s: None
    return c
