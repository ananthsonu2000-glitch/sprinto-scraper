"""Approval gate for every outgoing email.

With REQUIRE_APPROVAL on (the default for the running app), the agent never sends directly:
it puts the email here and a person approves (optionally after editing), saves or discards it.
Approval calls the same send path as before, with the same idempotency key (op_id), so the
delivery guard, the send ledger and the demo-recipient rule all still apply.
"""
from __future__ import annotations

import json

from . import clock, store
from .store import Stage

PURPOSE_LABEL = {
    "initial_outreach": "First email",
    "propose_times": "Proposes meeting times",
    "info_answer": "Answers a question",
    "clarify": "Asks to clarify",
    "booking_confirmation": "Confirms the booked meeting",
    "follow_up": "Follow-up",
}


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["context"] = json.loads(d["context"] or "{}")
    d["purpose_label"] = PURPOSE_LABEL.get(d["purpose"], d["purpose"].replace("_", " ").capitalize())
    return d


def queue(ctx, p: dict, op_id: str, purpose: str, subject: str, body: str, context: dict | None = None) -> dict:
    """Idempotent: the same op_id is queued once (a re-run of the agent step returns the existing row)."""
    conn = ctx.conn
    existing = conn.execute("SELECT * FROM outbox WHERE op_id=?", (op_id,)).fetchone()
    if existing:
        return _row(existing)
    oid = store.new_id("out")
    conn.execute(
        "INSERT INTO outbox(id,prospect_id,op_id,purpose,subject,body,original_body,status,context,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, p["id"], op_id, purpose, subject, body, body, "pending", json.dumps(context or {}, default=str),
         clock.iso(clock.now())))
    store.log_activity(conn, p["id"], "queue_for_approval", "waiting_for_approval",
                       f"{PURPOSE_LABEL.get(purpose, purpose)} waits for human approval", tool="outbox",
                       campaign_id=p["campaign_id"])
    return get(conn, oid)


def get(conn, oid: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM outbox WHERE id=?", (oid,)).fetchone())


def listing(conn, status: str | None = "pending", prospect_id: str | None = None) -> list[dict]:
    q = ("SELECT o.*, p.normalized_domain, p.contact, p.company_profile, p.stage FROM outbox o"
         " JOIN prospects p ON p.id=o.prospect_id WHERE 1=1")
    args: list = []
    if status:
        q += " AND o.status=?"
        args.append(status)
    if prospect_id:
        q += " AND o.prospect_id=?"
        args.append(prospect_id)
    out = []
    for r in conn.execute(q + " ORDER BY o.created_at", args):
        d = _row(r)
        contact = json.loads(d.pop("contact") or "null") or {}
        prof = json.loads(d.pop("company_profile") or "{}")
        name = (prof.get("company_name") or {}).get("value") or d["normalized_domain"]
        d["company"] = name[0] if isinstance(name, list) else name
        d["contact_name"] = contact.get("name")
        d["contact_title"] = contact.get("title")
        out.append(d)
    return out


class OutboxError(Exception):
    pass


def _pending(conn, oid) -> dict:
    row = get(conn, oid)
    if row is None:
        raise OutboxError("unknown email")
    if row["status"] != "pending":
        raise OutboxError(f"this email is already {row['status']}")
    return row


def save(ctx, oid: str, subject: str | None, body: str | None) -> dict:
    row = _pending(ctx.conn, oid)
    ctx.conn.execute("UPDATE outbox SET subject=?, body=? WHERE id=?",
                     ((subject or row["subject"]).strip(), (body or row["body"]).strip(), oid))
    return get(ctx.conn, oid)


def approve(ctx, oid: str, subject: str | None = None, body: str | None = None, note: str = "") -> dict:
    """Send it now. On failure the email goes back to pending with the error, so it can be retried."""
    row = save(ctx, oid, subject, body) if (subject or body) else _pending(ctx.conn, oid)
    edited = row["body"].strip() != row["original_body"].strip()
    p = store.get_prospect(ctx.conn, row["prospect_id"])
    store.log_activity(ctx.conn, p["id"], "approve_email", "approved",
                       f"{row['purpose_label']} approved{' after edits' if edited else ''}. {note}".strip(),
                       actor="human", tool="outbox", campaign_id=p["campaign_id"])
    if row["purpose"] == "initial_outreach":
        from .outreach import deliver_initial
        ok, error = deliver_initial(ctx, p, row["op_id"], row["subject"], row["body"])
    else:
        from .reply_agent import deliver_reply
        ok, error = deliver_reply(ctx, p, row["op_id"], row["purpose"], row["subject"], row["body"])
    now = clock.iso(clock.now())
    if ok:
        ctx.conn.execute("UPDATE outbox SET status='sent', decided_at=?, decided_note=?, error=NULL WHERE id=?",
                         (now, note or None, oid))
    else:
        ctx.conn.execute("UPDATE outbox SET error=? WHERE id=?", (error, oid))
    return get(ctx.conn, oid)


def discard(ctx, oid: str, note: str = "", redraft: bool = False) -> dict:
    """Drop the email. For a first email: redraft=True writes a new one on the next worker tick,
    otherwise the company is closed. For replies the conversation stays open for a manual answer."""
    row = _pending(ctx.conn, oid)
    p = store.get_prospect(ctx.conn, row["prospect_id"])
    ctx.conn.execute("UPDATE outbox SET status='discarded', decided_at=?, decided_note=? WHERE id=?",
                     (clock.iso(clock.now()), note or ("redraft requested" if redraft else None), oid))
    if row["purpose"] == "initial_outreach" and p["stage"] == Stage.AWAITING_APPROVAL:
        if redraft:
            # a fresh op_id per draft, so the new version is queued instead of matching this one
            store.save_prospect(ctx.conn, p, stage=Stage.PAIN_READY, draft=None, email_qa=None,
                                tool_attempts={**(p.get("tool_attempts") or {}), "redrafts": int((p.get("tool_attempts") or {}).get("redrafts", 0)) + 1})
        else:
            store.save_prospect(ctx.conn, p, stage=Stage.DONE, terminal_status="REJECTED_BY_REVIEWER")
    store.log_activity(ctx.conn, p["id"], "discard_email", "redraft" if redraft else "discarded",
                       f"{row['purpose_label']} discarded. {note}".strip(), actor="human", tool="outbox",
                       campaign_id=p["campaign_id"])
    return get(ctx.conn, oid)
