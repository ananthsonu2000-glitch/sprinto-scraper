"""Read models for the dashboard: funnel overview, email threads and the per-conversation summary.

Everything here is computed from stored state (no LLM call), so it is instant, free and always
consistent with what actually happened.
"""
from __future__ import annotations

from datetime import datetime

from . import clock, outbox, store
from .store import Stage

INTENT_LABEL = {
    "interested_meeting": "is interested in a meeting", "info_request": "asked for more information",
    "confirm_slot": "picked a time", "counterproposal": "suggested another time", "reject": "declined",
    "opt_out": "asked not to be contacted", "referral": "referred someone else", "out_of_office": "is out of office",
    "delivery_notice": "bounce / delivery notice", "ambiguous": "replied unclearly", "unsupported_request": "asked something the agent cannot answer",
}

# Funnel steps shown on the overview, in order. Each counts prospects that reached at least this step.
FUNNEL = [
    ("added", "Added", None),
    ("researched", "Researched", None),
    ("qualified", "Qualified", None),
    ("contact", "Contact found", None),
    ("drafted", "Email written", None),
    ("sent", "Email sent", None),
    ("replied", "Replied", None),
    ("booked", "Meeting booked", None),
]


def _company(p: dict) -> str:
    v = ((p.get("company_profile") or {}).get("company_name") or {}).get("value") or p["normalized_domain"]
    return v[0] if isinstance(v, list) else v


def _fmt(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        d = clock.parse(ts)
    except Exception:  # noqa: BLE001
        return ts
    return d.strftime("%d %b, %H:%M UTC")


def _slot(ctx, iso: str | None) -> str:
    if not iso:
        return ""
    try:
        from zoneinfo import ZoneInfo
        d: datetime = clock.parse(iso).astimezone(ZoneInfo(ctx.settings.organizer_timezone))
        return d.strftime("%a %d %b, %H:%M ") + ctx.settings.organizer_timezone
    except Exception:  # noqa: BLE001
        return iso


def _reached(p: dict, msgs_in: int) -> set[str]:
    s = {"added"}
    if p["research_rounds"] or p["qualification_status"]:
        s.add("researched")
    if p["qualification_status"] == "QUALIFIED" or p.get("contact"):
        s.add("qualified")
    if p.get("contact"):
        s.add("contact")
    if p.get("draft"):
        s.add("drafted")
    if p.get("gmail_thread_id"):
        s |= {"sent", "drafted"}
    if msgs_in:
        s.add("replied")
    if p.get("meeting_status") in ("BOOKED", "RESCHEDULED"):
        s.add("booked")
    return s


def funnel(ctx) -> dict:
    conn = ctx.conn
    inbound = {r[0]: r[1] for r in conn.execute(
        "SELECT prospect_id, COUNT(*) FROM messages WHERE direction='inbound' GROUP BY prospect_id")}
    counts = {k: 0 for k, _, _ in FUNNEL}
    stages: dict[str, int] = {}
    disq = 0
    for r in conn.execute("SELECT * FROM prospects"):
        p = store.row_to_dict(r)
        for k in _reached(p, inbound.get(p["id"], 0)):
            counts[k] += 1
        stages[p["stage"]] = stages.get(p["stage"], 0) + 1
        if p["qualification_status"] and p["qualification_status"] != "QUALIFIED" and not p.get("contact"):
            disq += 1
    pending = conn.execute("SELECT purpose, COUNT(*) FROM outbox WHERE status='pending' GROUP BY purpose").fetchall()
    approvals = sum(n for _, n in pending)
    unread = conn.execute("SELECT COUNT(DISTINCT prospect_id) FROM messages WHERE direction='inbound' "
                          "AND processing_status IN ('pending','processing')").fetchone()[0]
    escal = conn.execute("SELECT COUNT(*) FROM escalations WHERE status='open'").fetchone()[0]
    upcoming = [store.row_to_dict(r) for r in conn.execute(
        "SELECT * FROM prospects WHERE meeting_status IN ('BOOKED','RESCHEDULED') ORDER BY confirmed_slot")]
    return {
        "steps": [{"key": k, "label": label, "count": counts[k]} for k, label, _ in FUNNEL],
        "stages": stages,
        "disqualified": disq,
        "approvals": approvals,
        "approvals_by_purpose": {k: n for k, n in pending},
        "replies_waiting": unread,
        "escalations": escal,
        "in_progress": sum(stages.get(s, 0) for s in Stage.OUTREACH_ACTIVE),
        "awaiting_reply": stages.get(Stage.AWAITING_REPLY, 0),
        "meetings": [{"id": p["id"], "company": _company(p), "contact": (p.get("contact") or {}).get("name"),
                      "slot": _slot(ctx, p.get("confirmed_slot"))} for p in upcoming],
    }


def threads(ctx) -> list[dict]:
    """One entry per company that has an email (sent or waiting for approval)."""
    conn = ctx.conn
    out = []
    rows = conn.execute(
        "SELECT p.* FROM prospects p WHERE p.gmail_thread_id IS NOT NULL OR EXISTS "
        "(SELECT 1 FROM outbox o WHERE o.prospect_id=p.id) OR EXISTS (SELECT 1 FROM messages m WHERE m.prospect_id=p.id)")
    for r in rows:
        p = store.row_to_dict(r)
        msgs = store.list_messages(conn, p["id"])
        pend = outbox.listing(conn, "pending", p["id"])
        last = msgs[-1] if msgs else None
        last_ts = max([m["sent_at"] for m in msgs] + [o["created_at"] for o in pend] or [p["updated_at"]])
        snippet = (pend[-1]["body"] if pend and (not last or pend[-1]["created_at"] >= last["sent_at"]) else
                   (last["body"] if last else ""))
        out.append({
            "id": p["id"], "company": _company(p), "domain": p["normalized_domain"],
            "contact": (p.get("contact") or {}).get("name"), "subject": (p.get("draft") or {}).get("subject"),
            "snippet": " ".join((snippet or "").split())[:140], "last_at": last_ts,
            "messages": len(msgs), "pending_approval": len(pend),
            "unread": any(m["direction"] == "inbound" and m["processing_status"] in ("pending", "processing") for m in msgs),
            "last_from": ("draft" if pend and (not last or pend[-1]["created_at"] >= last["sent_at"])
                          else (last["direction"] if last else None)),
            "stage": p["stage"], "reply_status": p.get("reply_status"), "meeting_status": p.get("meeting_status"),
        })
    return sorted(out, key=lambda t: t["last_at"], reverse=True)


def _profile_facts(p: dict) -> list[dict]:
    keys = [("product", "Product"), ("customer_segments", "Sells to"), ("employee_count", "Employees"),
            ("funding_stage", "Funding"), ("compliance_claims", "Compliance"), ("hiring_signals", "Hiring")]
    prof = p.get("company_profile") or {}
    facts = []
    for k, label in keys:
        v = prof.get(k) or {}
        if v.get("value") not in (None, "", [], "unknown"):
            val = v.get("value")
            if isinstance(val, list):
                val = ", ".join(map(str, val[:3]))
            if val not in (None, "", "unknown"):
                facts.append({"label": label, "value": str(val)[:80]})
    return facts


def overview(ctx, pid: str) -> dict:
    conn = ctx.conn
    p = store.get_prospect(conn, pid)
    if not p:
        raise KeyError(pid)
    msgs = store.list_messages(conn, pid)
    pend = outbox.listing(conn, "pending", pid)
    esc = store.open_escalations(conn)
    esc = [e for e in esc if e["prospect_id"] == pid]
    c = p.get("contact") or {}
    pains = p.get("pain_hypotheses") or []

    story = []
    for m in msgs:
        if m["direction"] == "outbound":
            story.append({"at": m["sent_at"], "who": "us", "text": f"Email sent: “{(m['subject'] or '').strip()}”"})
        else:
            intent = (m.get("decision") or "").split(":")[0].strip()
            what = INTENT_LABEL.get(intent, "replied")
            excerpt = " ".join((m["body"] or "").split())[:120]
            story.append({"at": m["sent_at"], "who": "them",
                          "text": f"{c.get('first_name') or 'They'} {what}: “{excerpt}”" if intent else f"Reply received: “{excerpt}”"})
    for o in pend:
        story.append({"at": o["created_at"], "who": "draft", "text": f"Agent drafted: {o['purpose_label'].lower()} (waiting for your approval)"})
    if p.get("confirmed_slot"):
        story.append({"at": p["updated_at"], "who": "system", "text": f"Meeting booked for {_slot(ctx, p['confirmed_slot'])}"})
    story.sort(key=lambda s: s["at"])

    actions = []
    for o in pend:
        actions.append({"kind": "approve", "target": o["id"], "label": f"Review and approve: {o['purpose_label'].lower()}",
                        "detail": "Nothing is sent until you approve it. You can edit the text first."})
    for e in esc:
        actions.append({"kind": "escalation", "target": e["id"], "label": f"Decide: {e['reason'].replace('_', ' ').lower()}",
                        "detail": (e.get("details") or "")[:200], "options": e["allowed_actions"]})
    if any(m["direction"] == "inbound" and m["processing_status"] in ("pending", "processing") for m in msgs):
        actions.append({"kind": "wait_agent", "label": "The agent is reading their latest reply",
                        "detail": "It drafts an answer on the next worker run."})
    stage = p["stage"]
    if not actions:
        if p.get("meeting_status") in ("BOOKED", "RESCHEDULED"):
            actions.append({"kind": "meeting", "label": f"Prepare for the meeting on {_slot(ctx, p.get('confirmed_slot'))}",
                            "detail": "Use the pain hypotheses and sources below as talking points."})
        elif stage == Stage.AWAITING_REPLY:
            actions.append({"kind": "wait", "label": "Wait for their reply",
                            "detail": f"No automatic chase is sent. If nothing arrives by {_fmt(p.get('reply_deadline'))}, it comes back to you."})
            if ctx.settings.mode != "real_demo":
                actions.append({"kind": "simulate", "label": "Simulate their reply (testing only)", "detail": "Simulation mode: type what the prospect says."})
        elif stage in Stage.OUTREACH_ACTIVE:
            actions.append({"kind": "progress", "label": "The agent is still working on this company",
                            "detail": f"Current step: {stage.replace('_', ' ').lower()}."})
        elif p.get("terminal_status"):
            actions.append({"kind": "closed", "label": f"Closed: {p['terminal_status'].replace('_', ' ').lower()}", "detail": ""})

    if pend:
        headline = f"{len(pend)} email{'s' if len(pend) > 1 else ''} waiting for your approval"
    elif esc:
        headline = "Paused, needs your decision"
    elif p.get("meeting_status") in ("BOOKED", "RESCHEDULED"):
        headline = f"Meeting booked · {_slot(ctx, p.get('confirmed_slot'))}"
    elif stage == Stage.AWAITING_REPLY:
        headline = "Waiting for their reply"
    elif p.get("terminal_status"):
        headline = p["terminal_status"].replace("_", " ").capitalize()
    else:
        headline = stage.replace("_", " ").capitalize()

    return {
        "id": pid, "company": _company(p), "domain": p["normalized_domain"], "stage": stage, "headline": headline,
        "contact": {"name": c.get("name"), "title": c.get("title"), "role": c.get("role"),
                    "why": c.get("selection_reason")} if c else None,
        "qualification": {"status": p.get("qualification_status"), "score": p.get("qualification_score")},
        "facts": _profile_facts(p),
        "angle": pains[0]["hypothesis"] if pains else None,
        "pains": [x["hypothesis"] for x in pains[:3]],
        "proposed_slots": [t.get("display") for t in (p.get("times_proposed") or [])],
        "meeting": _slot(ctx, p.get("confirmed_slot")) if p.get("confirmed_slot") else None,
        "story": story,
        "next_actions": actions,
        "mode": ctx.settings.mode,
    }
