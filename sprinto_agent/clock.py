"""Injectable clock so tests and simulations are deterministic."""
from __future__ import annotations

from datetime import datetime, timezone

_override: datetime | None = None


def now() -> datetime:
    return _override if _override is not None else datetime.now(timezone.utc)


def set_now(dt: datetime | None) -> None:
    global _override
    _override = dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
