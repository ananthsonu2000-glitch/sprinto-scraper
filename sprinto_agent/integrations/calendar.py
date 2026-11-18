"""Organizer calendar: availability and booking.

We only ever read the ORGANIZER's free/busy. We never claim knowledge of a
prospect's calendar. Attendees are enforced to be exactly the demo address.
Events carry a private extended property `sprintoOpId` so uncertain bookings can
be reconciled by operation ID instead of blindly re-created.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import httpx

from .. import clock
from ..errors import ErrorKind, IntegrationError
from .delivery_guard import enforce_attendees, enforce_event_summary
from .google_auth import GoogleAuth, classify_google

CAL = "https://www.googleapis.com/calendar/v3"


class SimCalendar:
    label = "SIMULATED organizer calendar (local DB)"
    event_label = "SIMULATED"
    simulated = True

    def __init__(self, conn, settings):
        self.conn = conn
        self.settings = settings
        self.freebusy_calls = 0
        self.fail_freebusy: list[str] = []   # tests: "transient" | "auth"
        self.fail_create: list[str] = []     # tests: "permanent" | "timeout_after_create" | "timeout_before_create"
        self.lookup_fails = False

    def add_busy(self, start: datetime, end: datetime, label: str = "busy"):
        self.conn.execute("INSERT INTO sim_calendar_busy(start,end,label) VALUES (?,?,?)",
                          (clock.iso(start), clock.iso(end), label))

    def freebusy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        self.freebusy_calls += 1
        if self.fail_freebusy:
            mode = self.fail_freebusy.pop(0)
            kind = ErrorKind.AUTH if mode == "auth" else ErrorKind.TRANSIENT
            raise IntegrationError(kind, f"simulated calendar {mode} failure", "calendar")
        busy = [(clock.parse(r["start"]), clock.parse(r["end"])) for r in
                self.conn.execute("SELECT start,end FROM sim_calendar_busy WHERE end > ? AND start < ?",
                                  (clock.iso(start), clock.iso(end)))]
        busy += [(clock.parse(r["start"]), clock.parse(r["end"])) for r in
                 self.conn.execute("SELECT start,end FROM sim_calendar_events WHERE end > ? AND start < ?",
                                   (clock.iso(start), clock.iso(end)))]
        return busy

    def create_event(self, op_id, summary, start, end, attendees, description="") -> str:
        enforce_attendees(self.settings, attendees)
        enforce_event_summary(summary)
        mode = self.fail_create.pop(0) if self.fail_create else None
        if mode == "permanent":
            raise IntegrationError(ErrorKind.PERMANENT, "simulated calendar rejection", "calendar")
        if mode == "timeout_before_create":
            raise IntegrationError(ErrorKind.UNCERTAIN, "simulated timeout (not created)", "calendar")
        eid = f"simevt_{uuid.uuid4().hex[:10]}"
        now = clock.iso(clock.now())
        self.conn.execute(
            "INSERT INTO sim_calendar_events(event_id,op_id,summary,start,end,attendees,label,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, op_id, summary, clock.iso(start), clock.iso(end), json.dumps(attendees), self.event_label, now, now))
        if mode == "timeout_after_create":
            raise IntegrationError(ErrorKind.UNCERTAIN, "simulated timeout (actually created)", "calendar")
        return eid

    def find_event_by_op(self, op_id) -> str | None:
        if self.lookup_fails:
            raise IntegrationError(ErrorKind.TRANSIENT, "simulated lookup failure", "calendar")
        r = self.conn.execute("SELECT event_id FROM sim_calendar_events WHERE op_id=?", (op_id,)).fetchone()
        return r["event_id"] if r else None

    def update_event(self, event_id, op_id, summary, start, end, attendees) -> str:
        enforce_attendees(self.settings, attendees)
        enforce_event_summary(summary)
        cur = self.conn.execute(
            "UPDATE sim_calendar_events SET start=?, end=?, summary=?, op_id=?, updated_at=? WHERE event_id=?",
            (clock.iso(start), clock.iso(end), summary, op_id, clock.iso(clock.now()), event_id))
        if cur.rowcount != 1:
            raise IntegrationError(ErrorKind.INVALID_INPUT, f"event {event_id} not found", "calendar")
        return event_id

    def find_update_by_op(self, op_id) -> str | None:
        return self.find_event_by_op(op_id)


class DryRunCalendar(SimCalendar):
    """Reads availability from `reader` (real Google calendar if configured, else simulated);
    writes are recorded locally and labelled DRY RUN — no real events are created."""
    label = "DRY RUN calendar (real reads if configured; bookings NOT created)"
    event_label = "DRY RUN — NOT CREATED"

    def __init__(self, conn, settings, reader=None):
        super().__init__(conn, settings)
        self.reader = reader

    def freebusy(self, start, end):
        if self.reader is not None:
            self.freebusy_calls += 1
            return self.reader.freebusy(start, end)
        return super().freebusy(start, end)


class UnconfiguredCalendar:
    label = "Google Calendar NOT CONFIGURED"
    simulated = False

    def __init__(self, reason):
        self.reason = reason

    def _fail(self, *a, **k):
        raise IntegrationError(ErrorKind.SETUP_REQUIRED, self.reason, "calendar")

    freebusy = create_event = find_event_by_op = update_event = find_update_by_op = _fail


class GoogleCalendar:
    label = "Google Calendar (REAL — demo attendee only)"
    simulated = False

    def __init__(self, settings, auth: GoogleAuth, http: httpx.Client | None = None):
        self.settings = settings
        self.auth = auth
        self.http = http or httpx.Client(timeout=20)
        self.cal_id = settings.google_calendar_id
        self.freebusy_calls = 0

    def _req(self, method, url, irreversible=False, **kw):
        try:
            r = self.http.request(method, url, headers=self.auth.headers(), **kw)
        except httpx.TimeoutException as e:
            kind = ErrorKind.UNCERTAIN if irreversible else ErrorKind.TRANSIENT
            raise IntegrationError(kind, f"calendar timeout: {e}", "calendar")
        except httpx.TransportError as e:
            kind = ErrorKind.UNCERTAIN if irreversible else ErrorKind.TRANSIENT
            raise IntegrationError(kind, f"calendar transport error: {e}", "calendar")
        if r.status_code not in (200, 201):
            raise classify_google(r, "calendar", irreversible=irreversible)
        return r.json()

    def freebusy(self, start, end):
        self.freebusy_calls += 1
        d = self._req("POST", f"{CAL}/freeBusy", json={
            "timeMin": clock.iso(start), "timeMax": clock.iso(end), "items": [{"id": self.cal_id}]})
        cal = d.get("calendars", {}).get(self.cal_id, {})
        if cal.get("errors"):
            raise IntegrationError(ErrorKind.PERMANENT, f"freebusy error: {cal['errors']}", "calendar")
        return [(clock.parse(b["start"]), clock.parse(b["end"])) for b in cal.get("busy", [])]

    def _event_body(self, op_id, summary, start, end, attendees, description=""):
        return {
            "summary": summary, "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": self.settings.organizer_timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.settings.organizer_timezone},
            "attendees": [{"email": a} for a in attendees],
            "extendedProperties": {"private": {"sprintoOpId": op_id}},
        }

    def create_event(self, op_id, summary, start, end, attendees, description="") -> str:
        attendees = enforce_attendees(self.settings, attendees)
        enforce_event_summary(summary)
        d = self._req("POST", f"{CAL}/calendars/{self.cal_id}/events", irreversible=True,
                      params={"sendUpdates": "all"},
                      json=self._event_body(op_id, summary, start, end, attendees, description))
        return d["id"]

    def find_event_by_op(self, op_id) -> str | None:
        d = self._req("GET", f"{CAL}/calendars/{self.cal_id}/events",
                      params={"privateExtendedProperty": f"sprintoOpId={op_id}", "showDeleted": "false"})
        items = d.get("items", [])
        return items[0]["id"] if items else None

    find_update_by_op = find_event_by_op

    def update_event(self, event_id, op_id, summary, start, end, attendees) -> str:
        attendees = enforce_attendees(self.settings, attendees)
        enforce_event_summary(summary)
        d = self._req("PATCH", f"{CAL}/calendars/{self.cal_id}/events/{event_id}", irreversible=True,
                      params={"sendUpdates": "all"},
                      json=self._event_body(op_id, summary, start, end, attendees))
        return d["id"]
