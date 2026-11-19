"""Persistence helpers: prospects (optimistic concurrency + leases), evidence,
activity log ("Demo activity log"), escalations, messages, integration status."""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

from . import clock

log = logging.getLogger(__name__)

JSON_FIELDS = {
    "company_profile", "evidence_ids", "research_log", "qualification_detail", "contact",
    "contact_candidates", "referrals", "pain_hypotheses", "pain_detail", "draft", "email_qa",
    "outbound_message_ids", "times_proposed", "rejected_slots", "tool_attempts",
}
IMMUTABLE = {"id", "campaign_id", "normalized_domain", "created_at", "version", "lease_owner", "lease_until"}


class Stage:
    QUEUED = "QUEUED"
    RESEARCHING = "RESEARCHING"
    QUALIFIED = "QUALIFIED"
    CONTACT_SELECTED = "CONTACT_SELECTED"
    PAIN_READY = "PAIN_READY"
    DRAFT_READY = "DRAFT_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # outgoing email waits in the outbox for a person
    AWAITING_REPLY = "AWAITING_REPLY"
    MEETING_BOOKED = "MEETING_BOOKED"
    WAITING_INTEGRATION = "WAITING_INTEGRATION"
    ESCALATED = "ESCALATED"
    DONE = "DONE"

    OUTREACH_ACTIVE = (QUEUED, RESEARCHING, QUALIFIED, CONTACT_SELECTED, PAIN_READY, DRAFT_READY)
    CONVERSATION = (AWAITING_REPLY, MEETING_BOOKED)


class StaleStateError(Exception):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dump(v):
    return None if v is None else json.dumps(v, default=str)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


# ---------------------------------------------------------------- prospects

def get_prospect(conn, pid: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone())


def save_prospect(conn, p: dict, **changes) -> dict:
    """Optimistic-concurrency update. Raises StaleStateError if someone else wrote first."""
    bad = IMMUTABLE & changes.keys()
    if bad:
        raise ValueError(f"cannot change {bad}")
    if not changes:
        return p
    now = clock.iso(clock.now())
    cols = ", ".join(f"{k}=?" for k in changes) + ", version=version+1, updated_at=?"
    vals = [(_dump(v) if k in JSON_FIELDS else v) for k, v in changes.items()]
    cur = conn.execute(
        f"UPDATE prospects SET {cols} WHERE id=? AND version=?",
        (*vals, now, p["id"], p["version"]),
    )
    if cur.rowcount != 1:
        raise StaleStateError(f"prospect {p['id']} changed concurrently (expected v{p['version']})")
    p.update(changes)
    p["version"] += 1
    p["updated_at"] = now
    return p


def acquire_lease(conn, pid: str, owner: str, seconds: int) -> bool:
    now = clock.now()
    cur = conn.execute(
        "UPDATE prospects SET lease_owner=?, lease_until=? WHERE id=? AND "
        "(lease_owner IS NULL OR lease_owner=? OR lease_until IS NULL OR lease_until < ?)",
        (owner, clock.iso(now + timedelta(seconds=seconds)), pid, owner, clock.iso(now)),
    )
    return cur.rowcount == 1


def release_lease(conn, pid: str, owner: str) -> None:
    conn.execute("UPDATE prospects SET lease_owner=NULL, lease_until=NULL WHERE id=? AND lease_owner=?", (pid, owner))


# ---------------------------------------------------------------- activity log (Demo activity log)

FALLBACK_LOG = Path(__file__).resolve().parent.parent / "data" / "activity_fallback.jsonl"


def log_activity(conn, prospect_id: str | None, action: str, status: str, reason: str = "",
                 tool: str | None = None, details: dict | None = None, actor: str = "agent",
                 campaign_id: str | None = None) -> bool:
    """Write to the internal Demo activity log. Never raises: a logging failure must not
    undo a successful action. Failed writes go to a fallback file and are replayed later."""
    entry = (clock.iso(clock.now()), campaign_id, prospect_id, actor, action, tool, status,
             reason[:500], json.dumps(_redact(details or {}), default=str))
    for _ in range(3):
        try:
            conn.execute(
                "INSERT INTO activity_log(ts,campaign_id,prospect_id,actor,action,tool,status,reason,details)"
                " VALUES (?,?,?,?,?,?,?,?,?)", entry)
            return True
        except sqlite3.Error as e:  # pragma: no cover - exercised via monkeypatch in tests
            last = e
    try:
        FALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FALLBACK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        log.error("activity log write failed: %s", last)
    return False


def replay_fallback_log(conn) -> int:
    if not FALLBACK_LOG.exists():
        return 0
    lines = FALLBACK_LOG.read_text(encoding="utf-8").splitlines()
    n = 0
    remaining = []
    for line in lines:
        try:
            conn.execute(
                "INSERT INTO activity_log(ts,campaign_id,prospect_id,actor,action,tool,status,reason,details)"
                " VALUES (?,?,?,?,?,?,?,?,?)", tuple(json.loads(line)))
            n += 1
        except sqlite3.Error:
            remaining.append(line)
    FALLBACK_LOG.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
    return n


SECRET_KEYS = ("api_key", "token", "secret", "password", "authorization")


def _redact(d):
    if isinstance(d, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in SECRET_KEYS) else _redact(v)) for k, v in d.items()}
    if isinstance(d, list):
        return [_redact(x) for x in d]
    return d


# ---------------------------------------------------------------- evidence

def add_evidence(conn, prospect_id: str, page: dict, excerpt: str, field: str | None,
                 published_at: str | None, source_type: str = "company_website_first_party") -> str:
    row = conn.execute(
        "SELECT id FROM evidence WHERE prospect_id=? AND source_url=? AND excerpt=?",
        (prospect_id, page["final_url"] or page["url"], excerpt)).fetchone()
    if row:
        return row["id"]
    eid = new_id("ev")
    conn.execute(
        "INSERT INTO evidence(id,prospect_id,page_id,source_url,excerpt,retrieved_at,published_at,source_type,field)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, prospect_id, page["id"], page["final_url"] or page["url"], excerpt, page["retrieved_at"],
         published_at, source_type, field))
    return eid


def evidence_map(conn, prospect_id: str) -> dict[str, dict]:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM evidence WHERE prospect_id=?", (prospect_id,))}


# ---------------------------------------------------------------- pages

def list_pages(conn, prospect_id: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM pages WHERE prospect_id=? ORDER BY retrieved_at, rowid", (prospect_id,)):
        d = dict(r)
        d["links"] = json.loads(d["links"] or "[]")
        out.append(d)
    return out


# ---------------------------------------------------------------- escalations

ALLOWED_ACTIONS = {
    "SEND_UNCERTAIN": ["confirm_sent", "confirm_not_sent", "close"],
    "BOOKING_UNCERTAIN": ["confirm_booked", "confirm_not_booked", "close"],
    "NO_REPLY": ["keep_waiting", "close"],
}
DEFAULT_ACTIONS = ["resume", "close"]


def open_escalation(conn, p: dict, reason: str, details: str) -> dict:
    """Pause ONLY this prospect. Records the stage to resume to."""
    esc_id = new_id("esc")
    conn.execute(
        "INSERT INTO escalations(id,prospect_id,reason,details,status,allowed_actions,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (esc_id, p["id"], reason, details[:2000], "open",
         json.dumps(ALLOWED_ACTIONS.get(reason, DEFAULT_ACTIONS)), clock.iso(clock.now())))
    paused = p["stage"] if p["stage"] != Stage.ESCALATED else p.get("paused_stage")
    save_prospect(conn, p, stage=Stage.ESCALATED, paused_stage=paused, escalation_reason=f"{reason}: {details}"[:500])
    log_activity(conn, p["id"], "escalate", "paused", f"{reason}: {details}", tool="escalate",
                 campaign_id=p["campaign_id"])
    return {"id": esc_id, "reason": reason}


def open_escalations(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT e.*, p.normalized_domain FROM escalations e JOIN prospects p ON p.id=e.prospect_id"
        " WHERE e.status='open' ORDER BY e.created_at").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["allowed_actions"] = json.loads(d["allowed_actions"])
        out.append(d)
    return out


# ---------------------------------------------------------------- integration status

def set_integration_status(conn, name: str, status: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO integration_status(name,status,detail,updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at",
        (name, status, detail[:500], clock.iso(clock.now())))


def get_integration_status(conn, name: str) -> dict | None:
    r = conn.execute("SELECT * FROM integration_status WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- messages

def record_message(conn, prospect_id, provider_message_id, thread_id, direction, author_kind,
                   subject, body, sent_at=None, processing_status="n/a", delivery_label=None) -> bool:
    """Insert once per provider message ID. Returns False if it already existed (dedupe)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages(prospect_id,provider_message_id,thread_id,direction,author_kind,subject,"
        "body,sent_at,processing_status,delivery_label,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (prospect_id, provider_message_id, thread_id, direction, author_kind, subject, body,
         sent_at or clock.iso(clock.now()), processing_status, delivery_label, clock.iso(clock.now())))
    return cur.rowcount == 1


def list_messages(conn, prospect_id) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM messages WHERE prospect_id=? ORDER BY id", (prospect_id,))]
