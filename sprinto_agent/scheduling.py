"""Slot generation, explicit date/time/timezone formatting, and safe parsing of
prospect-proposed times.

We never assume the prospect's timezone from the organizer's. A proposed time is
only resolved when the date, the hour, and the timezone are all explicit (or the
prospect's timezone was explicitly stated earlier in the thread); otherwise the
agent asks for clarification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dparser

from . import clock

TZ_ALIASES = {
    "utc": "UTC", "gmt": "UTC",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pacific": "America/Los_Angeles",
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "eastern": "America/New_York",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin",
    "sgt": "Asia/Singapore", "aest": "Australia/Sydney", "aedt": "Australia/Sydney", "jst": "Asia/Tokyo",
    "india time": "Asia/Kolkata", "indian standard time": "Asia/Kolkata",
    "uk time": "Europe/London", "london time": "Europe/London",
}
AMBIGUOUS_TZ = {"ist", "cst", "bst", "ast", "cdt"}  # several real-world meanings
TZ_TOKEN = re.compile(r"\b(india time|indian standard time|uk time|london time|[A-Za-z]+/[A-Za-z_]+(?:/[A-Za-z_]+)?|[A-Za-z]{1,5}|pacific|eastern)\b")


@dataclass
class ParseResult:
    ok: bool
    start: datetime | None = None
    timezone: str | None = None
    reason: str = ""


def resolve_timezone(tz_text: str | None) -> tuple[str | None, str]:
    """Returns (IANA name, problem). problem is '' when resolved."""
    if not tz_text:
        return None, "missing"
    t = tz_text.strip().lower()
    if t in AMBIGUOUS_TZ:
        return None, f"'{tz_text}' is ambiguous"
    if t in TZ_ALIASES:
        return TZ_ALIASES[t], ""
    try:
        ZoneInfo(tz_text.strip())
        return tz_text.strip(), ""
    except Exception:  # noqa: BLE001
        return None, f"unrecognised timezone '{tz_text}'"


def find_tz_in_text(text: str) -> str | None:
    for m in TZ_TOKEN.finditer(text):
        tok = m.group(1)
        low = tok.lower()
        if low in TZ_ALIASES or low in AMBIGUOUS_TZ:
            return tok
        if "/" in tok:
            try:
                ZoneInfo(tok)
                return tok
            except Exception:  # noqa: BLE001
                continue
    return None


def parse_proposed_time(text: str, tz_text: str | None, known_prospect_tz: str | None, now: datetime) -> ParseResult:
    tz_text = tz_text or find_tz_in_text(text)
    if tz_text:
        tz, problem = resolve_timezone(tz_text)
        if problem:
            return ParseResult(False, reason=f"timezone {problem}")
    elif known_prospect_tz:
        tz = known_prospect_tz
    else:
        return ParseResult(False, reason="no timezone given")
    cleaned = text
    if tz_text:
        cleaned = re.sub(re.escape(tz_text), " ", cleaned, flags=re.I)
    d1, d2 = datetime(2000, 1, 1, 0, 0), datetime(2001, 2, 2, 13, 0)
    try:
        p1 = dparser.parse(cleaned, fuzzy=True, default=d1)
        p2 = dparser.parse(cleaned, fuzzy=True, default=d2)
    except (ValueError, OverflowError):
        return ParseResult(False, reason="could not read a date and time")
    if (p1.month, p1.day) != (p2.month, p2.day):
        return ParseResult(False, reason="no explicit calendar date (e.g. 'Nov 25')")
    if p1.hour != p2.hour:
        return ParseResult(False, reason="no explicit time of day")
    year = p1.year if p1.year == p2.year else None
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    y = year or local_now.year
    cand = datetime(y, p1.month, p1.day, p1.hour, p1.minute, tzinfo=zone)
    if year is None and cand < local_now - timedelta(days=1):
        cand = cand.replace(year=y + 1)
    return ParseResult(True, start=cand.astimezone(timezone.utc), timezone=tz)


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def within_working_hours(start: datetime, settings) -> tuple[bool, str]:
    z = ZoneInfo(settings.organizer_timezone)
    ls = start.astimezone(z)
    le = ls + timedelta(minutes=settings.meeting_minutes)
    if ls.weekday() >= 5:
        return False, "falls on a weekend in the organizer's timezone"
    if ls.time() < _hm(settings.working_hours_start) or le.time() > _hm(settings.working_hours_end) or le.date() != ls.date():
        return False, f"outside organizer working hours {settings.working_hours_start}-{settings.working_hours_end} {settings.organizer_timezone}"
    now = clock.now()
    if start <= now + timedelta(hours=2):
        return False, "too soon or in the past"
    if start > now + timedelta(days=settings.booking_horizon_days):
        return False, f"beyond the {settings.booking_horizon_days}-day booking horizon"
    return True, ""


def is_free(start: datetime, minutes: int, busy: list[tuple[datetime, datetime]]) -> bool:
    end = start + timedelta(minutes=minutes)
    return all(not (s < end and start < e) for s, e in busy)


@dataclass
class TimeWindow:
    """A prospect's general timing preference resolved to concrete organizer-calendar days/hours."""
    dates: list  # list[date] in the organizer's timezone
    hours: tuple[time, time] | None
    label: str   # what the prospect said, e.g. "next week"


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_RE = re.compile(r"\b(?:(next|this|coming)\s+)?(mon|tue|tues|wed|thu|thur|thurs|fri)(?:day|nesday|rsday|sday)?s?\b", re.I)
PART_OF_DAY = {"morning": (time(9, 0), time(12, 0)), "afternoon": (time(12, 0), time(17, 0)),
               "evening": (time(16, 0), time(18, 0))}


def resolve_window(text: str | None, settings, now: datetime | None = None) -> TimeWindow | None:
    """Turn 'next week', 'Tuesday afternoon', 'tomorrow morning', 'this week', 'week after next'
    into organizer-timezone working days (+ optional hours). Returns None if nothing recognisable."""
    if not text:
        return None
    t = text.lower()
    now = now or clock.now()
    z = ZoneInfo(settings.organizer_timezone)
    today = now.astimezone(z).date()
    wd = today.weekday()
    next_monday = today + timedelta(days=7 - wd)
    dates: list = []
    label = None
    if re.search(r"\bweek after next\b|\bin two weeks\b|\bin 2 weeks\b", t):
        start = next_monday + timedelta(days=7)
        dates, label = [start + timedelta(days=i) for i in range(5)], "the week after next"
    elif re.search(r"\b(next|following|coming) week\b|\bnext wk\b", t):
        dates, label = [next_monday + timedelta(days=i) for i in range(5)], "next week"
    elif re.search(r"\b(this|later this|rest of (the|this)) week\b", t):
        dates = [today + timedelta(days=i) for i in range(1, 5 - wd)]
        label = "later this week"
    elif re.search(r"\bday after tomorrow\b", t):
        dates, label = [today + timedelta(days=2)], "the day after tomorrow"
    elif re.search(r"\btomorrow\b", t):
        dates, label = [today + timedelta(days=1)], "tomorrow"
    else:
        found = []
        for m in DAY_RE.finditer(t):
            idx = ["mon", "tue", "wed", "thu", "fri"].index(m.group(2)[:3])
            ahead = (idx - wd) % 7 or 7
            d = today + timedelta(days=ahead)
            if (m.group(1) or "").lower() == "next" and d < next_monday:
                d += timedelta(days=7)
            found.append(d)
        if found:
            dates = sorted(set(found))
            label = " / ".join(WEEKDAYS[d.weekday()].capitalize() + " " + d.strftime("%d %b") for d in dates)
    hours = next((v for k, v in PART_OF_DAY.items() if re.search(rf"\b{k}s?\b", t)), None)
    if not dates and hours:  # "mornings work better" -> keep default days, restrict hours
        dates = []
        label = next(k + "s" for k in PART_OF_DAY if re.search(rf"\b{k}s?\b", t))
    if not dates and not hours:
        return None
    dates = [d for d in dates if d.weekday() < 5]
    if hours and label and not any(k in label for k in PART_OF_DAY):
        label += " (" + next(k for k, v in PART_OF_DAY.items() if v == hours) + ")"
    return TimeWindow(dates=dates, hours=hours, label=label or text.strip())


def candidate_slots(settings, busy, exclude: set[str], count: int, now: datetime | None = None,
                    window: TimeWindow | None = None) -> list[datetime]:
    """Earliest free, verified slots. Without a window: one per day from the next working day.
    With a window: only inside its days/hours (several per day if the window is short)."""
    now = now or clock.now()
    z = ZoneInfo(settings.organizer_timezone)
    preferred = [time(11, 0), time(15, 0), time(10, 0), time(16, 0), time(12, 0), time(14, 0)]
    if window and window.hours:
        h0, h1 = window.hours
        steps = []
        cur = datetime.combine(now.date(), h0)
        while cur.time() < h1 and cur.time() >= h0:
            steps.append(cur.time())
            cur += timedelta(minutes=30)
        preferred = [t for t in preferred if h0 <= t < h1] + [t for t in steps if t not in preferred]
    if window and window.dates:
        days = list(window.dates)
    else:
        start = (now.astimezone(z) + timedelta(days=1)).date()
        days = [start + timedelta(days=i) for i in range(settings.booking_horizon_days)]
    per_day = 1 if len(days) >= count else -(-count // max(1, len(days)))
    out: list[datetime] = []
    for day in days:
        if datetime.combine(day, time(0, 0), tzinfo=z).weekday() >= 5:
            continue
        picked = 0
        for t in preferred:
            s = datetime.combine(day, t, tzinfo=z).astimezone(timezone.utc)
            if clock.iso(s) in exclude or any(abs((s - o).total_seconds()) < 3600 for o in out):
                continue
            if within_working_hours(s, settings)[0] and is_free(s, settings.meeting_minutes, busy):
                out.append(s)
                picked += 1
                if picked >= per_day:
                    break
        if len(out) >= count:
            break
    return sorted(out[:count])


def fmt_slot(start: datetime, settings, prospect_tz: str | None = None) -> str:
    z = ZoneInfo(settings.organizer_timezone)
    ls = start.astimezone(z)
    le = ls + timedelta(minutes=settings.meeting_minutes)
    off = ls.strftime("%z")
    s = (f"{ls.strftime('%a, %d %b %Y, %H:%M')}–{le.strftime('%H:%M')} {settings.organizer_timezone} "
         f"(UTC{off[:3]}:{off[3:]})")
    utc = start.astimezone(timezone.utc)
    s += f" = {utc.strftime('%H:%M')} UTC"
    if prospect_tz and prospect_tz != settings.organizer_timezone:
        pz = ZoneInfo(prospect_tz)
        ps = start.astimezone(pz)
        s += f" = {ps.strftime('%a %H:%M')} {prospect_tz}"
    return s


def horizon_window(settings, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or clock.now()
    return now, now + timedelta(days=settings.booking_horizon_days + 1)
