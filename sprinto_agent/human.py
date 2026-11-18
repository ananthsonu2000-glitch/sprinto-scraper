"""Controlled human actions: resolving escalations and resuming paused integrations."""
from __future__ import annotations

import json
from datetime import timedelta

from . import clock, store
from .store import Stage


class HumanActionError(Exception):
    pass


def _latest_op(conn, pid, kinds, statuses=("uncertain", "pending")):
    q = (f"SELECT * FROM operations WHERE prospect_id=? AND kind IN ({','.join('?' * len(kinds))}) "
         f"AND status IN ({','.join('?' * len(statuses))}) ORDER BY updated_at DESC LIMIT 1")
    r = conn.execute(q, (pid, *kinds, *statuses)).fetchone()
    return dict(r) if r else None


def resolve_escalation(ctx, esc_id: str, action: str, note: str = "") -> dict:
    conn = ctx.conn
    row = conn.execute("SELECT * FROM escalations WHERE id=?", (esc_id,)).fetchone()
    if row is None or row["status"] != "open":
        raise HumanActionError("escalation not found or already resolved")
    allowed = json.loads(row["allowed_actions"])
    if action not in allowed:
        raise HumanActionError(f"action '{action}' not allowed; choose one of {allowed}")
    p = store.get_prospect(conn, row["prospect_id"])
    paused = p["paused_stage"] or Stage.QUEUED
    now = clock.now()
    changes: dict = {"escalation_reason": None, "paused_stage": None}

    if action == "close":
        changes.update(stage=Stage.DONE, terminal_status="CLOSED_BY_HUMAN")
    elif action == "resume":
        changes.update(stage=paused)
    elif action == "keep_waiting":
        changes.update(stage=paused, reply_deadline=clock.iso(now + timedelta(hours=ctx.settings.no_reply_handoff_hours)))
    elif action in ("confirm_sent", "confirm_not_sent"):
        op = _latest_op(conn, p["id"], ("send_email",))
        if op is None:
            raise HumanActionError("no uncertain send operation found")
        if action == "confirm_sent":
            conn.execute("UPDATE operations SET status='succeeded', provider_ref=?, error=?, updated_at=? WHERE op_id=?",
                         (json.dumps({"id": f"human-confirmed:{op['op_id']}", "thread_id": p.get("gmail_thread_id")}),
                          "confirmed by human", clock.iso(now), op["op_id"]))
            if paused == Stage.DRAFT_READY and not p.get("gmail_thread_id"):
                changes.update(stage=Stage.DONE, terminal_status="SENT_HUMAN_CONFIRMED_UNTRACKED")
            else:
                changes.update(stage=paused)
        else:
            conn.execute("UPDATE operations SET status='failed_retryable', attempts=0, error=?, updated_at=? WHERE op_id=?",
                         ("human confirmed not sent", clock.iso(now), op["op_id"]))
            changes.update(stage=paused)
    elif action in ("confirm_booked", "confirm_not_booked"):
        op = _latest_op(conn, p["id"], ("book_meeting", "update_meeting"))
        if op is None:
            raise HumanActionError("no uncertain booking operation found")
        if action == "confirm_booked":
            conn.execute("UPDATE operations SET status='succeeded', provider_ref=?, error=?, updated_at=? WHERE op_id=?",
                         (f"human-confirmed:{op['op_id']}", "confirmed by human", clock.iso(now), op["op_id"]))
            slot = json.loads(op["request"]).get("slot_start")
            changes.update(stage=Stage.MEETING_BOOKED, meeting_status="BOOKED_HUMAN_CONFIRMED", confirmed_slot=slot)
        else:
            conn.execute("UPDATE operations SET status='failed_retryable', attempts=0, error=?, updated_at=? WHERE op_id=?",
                         ("human confirmed not booked", clock.iso(now), op["op_id"]))
            changes.update(stage=paused)
    store.save_prospect(conn, p, **changes)
    conn.execute("UPDATE escalations SET status='resolved', resolution=?, resolution_note=?, resolved_at=? WHERE id=?",
                 (action, note[:500], clock.iso(now), esc_id))
    store.log_activity(conn, p["id"], "escalation_resolved", action, note or row["reason"], actor="human",
                       campaign_id=p["campaign_id"])
    return {"escalation_id": esc_id, "action": action, "prospect_stage": changes.get("stage")}


def resume_integration(ctx, name: str) -> int:
    conn = ctx.conn
    conn.execute("DELETE FROM integration_status WHERE name=?", (name,))
    n = 0
    for r in conn.execute("SELECT id FROM prospects WHERE stage=?", (Stage.WAITING_INTEGRATION,)).fetchall():
        p = store.get_prospect(conn, r["id"])
        store.save_prospect(conn, p, stage=p["paused_stage"] or Stage.QUEUED, paused_stage=None, last_error=None)
        n += 1
    store.log_activity(conn, None, "integration_resumed", "ok", f"{name}: {n} prospects resumed", actor="human")
    return n
