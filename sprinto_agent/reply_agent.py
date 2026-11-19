"""LangGraph reply & scheduling agent (one graph run per incoming message).

classify -> {interest | info | confirm | counter | reject | opt_out | referral | ooo |
             delivery_notice | handoff} -> finalize

* Durable checkpoints: SqliteSaver, thread_id = reply:{prospect}:{provider_message_id}.
  A crashed run is resumed from its last checkpoint on the next worker tick.
* Domain state (slots, rounds, meeting) lives in the prospects table, not in process memory.
* Every outbound email/booking uses a stable op ID, so re-running a node never repeats
  an irreversible action.
* Email templates for scheduling are deterministic code (explicit dates, times, timezones).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from . import clock, store
from .contacts import score_candidate
from .errors import ErrorKind
from .scheduling import (candidate_slots, fmt_slot, horizon_window, is_free, parse_proposed_time,
                         resolve_window, within_working_hours)
from .schemas import InfoAnswer, ReplyClassification
from .store import Stage
from .tools import run_tool

OPT_OUT_RE = re.compile(r"\b(unsubscribe|remove me|stop (emailing|contacting)|do not (email|contact)|opt(-| )?out)\b", re.I)
LABELS = "ABCDEFG"


class ReplyState(TypedDict, total=False):
    prospect_id: str
    message_db_id: int
    provider_message_id: str
    body: str
    intent: str
    classification: dict
    outcome: str
    note: str


# ------------------------------------------------------------------ helpers

def _first(p):
    return (p.get("contact") or {}).get("first_name") or "there"


def _sign(ctx):
    return f"\n\nBest,\n{ctx.settings.sender_display_name}"


def _send_reply(ctx, p: dict, st: ReplyState, action: str, purpose: str, body: str) -> tuple[dict, bool]:
    op_id = f"reply:{p['gmail_thread_id']}:{st['provider_message_id']}:{action}"
    subject = "Re: " + (p.get("draft") or {}).get("subject", "Sprinto")
    if ctx.settings.require_approval:
        from . import outbox
        c = st.get("classification") or {}
        outbox.queue(ctx, p, op_id, purpose, subject, body,
                     {"action": action, "in_reply_to": st.get("provider_message_id"),
                      "their_message": (st.get("body") or "")[:600], "intent": c.get("intent")})
        return store.get_prospect(ctx.conn, p["id"]), True
    ok, _ = deliver_reply(ctx, p, op_id, purpose, subject, body, action)
    return store.get_prospect(ctx.conn, p["id"]), ok


def deliver_reply(ctx, p: dict, op_id: str, purpose: str, subject: str, body: str,
                  action: str | None = None) -> tuple[bool, str | None]:
    res = run_tool(ctx, "send_email", {"prospect_id": p["id"], "op_id": op_id, "purpose": purpose,
                                       "subject": subject, "body": body}, reason=f"reply action: {action or purpose}")
    p = store.get_prospect(ctx.conn, p["id"])
    if not res.ok:
        reason = "SEND_UNCERTAIN" if res.error_kind == ErrorKind.UNCERTAIN else "SEND_FAILED"
        store.open_escalation(ctx.conn, p, reason, f"{action or purpose}: {res.error_kind}: {res.error}")
        return False, f"{res.error_kind}: {res.error}"
    deadline = clock.now() + timedelta(hours=ctx.settings.no_reply_handoff_hours)
    store.save_prospect(ctx.conn, p, outbound_message_ids=list(dict.fromkeys(p["outbound_message_ids"] + [res.data["provider_message_id"]])),
                        reply_deadline=clock.iso(deadline))
    return True, None


def _busy(ctx, p, start, end) -> list | None:
    res = run_tool(ctx, "check_calendar", {"prospect_id": p["id"], "window_start": clock.iso(start),
                                           "window_end": clock.iso(end)})
    if not res.ok:
        return None
    return [(clock.parse(s), clock.parse(e)) for s, e in res.data["busy"]]


def _escalate(ctx, p, reason, details) -> str:
    store.open_escalation(ctx.conn, store.get_prospect(ctx.conn, p["id"]), reason, details)
    return f"escalated:{reason}"


def _window_for(ctx, st: ReplyState):
    c = st.get("classification") or {}
    return resolve_window(c.get("preferred_window_text"), ctx.settings) or resolve_window(st.get("body"), ctx.settings)


def _propose(ctx, p: dict, st: ReplyState, intro: str, extra_exclude: set[str] | None = None, window=None) -> str:
    """Check the organizer calendar, then propose verified slots (never unverified).
    If the prospect stated a timing preference (window), only slots inside it are offered."""
    existing = [t for t in p["times_proposed"] if t.get("for_message") == st["provider_message_id"]]
    window_note = ""
    if existing:
        slots = [clock.parse(t["start"]) for t in existing]  # resume: reuse what this run already chose
    else:
        start, end = horizon_window(ctx.settings)
        busy = _busy(ctx, p, start, end)
        if busy is None:
            return _escalate(ctx, p, "CALENDAR_UNAVAILABLE",
                             "Availability check failed after retries; no unverified slots were proposed")
        exclude = set(p["rejected_slots"]) | (extra_exclude or set())
        n = ctx.policy["reply"]["slots_to_propose"]
        slots = candidate_slots(ctx.settings, busy, exclude, n, window=window) if window else []
        if window and slots:
            window_note = f"Since you mentioned {window.label}, "
        elif window:
            window_note = f"I don't have a free slot {window.label} on my side, so here are the nearest alternatives. "
        if not slots:
            slots = candidate_slots(ctx.settings, busy, exclude, n)
        if not slots:
            return _escalate(ctx, p, "NO_AVAILABILITY", "No free slots in the booking horizon")
        proposed = [{"label": LABELS[i], "start": clock.iso(s), "display": fmt_slot(s, ctx.settings, p["prospect_timezone"]),
                     "for_message": st["provider_message_id"]} for i, s in enumerate(slots)]
        p = store.save_prospect(ctx.conn, p, times_proposed=proposed, calendar_checked=1,
                                calendar_checked_at=clock.iso(clock.now()), checked_slot=None, requested_slot=None)
    lines = "\n".join(f"  Option {t['label']}: {t['display']}" for t in p["times_proposed"])
    lead = (f"{window_note}these times are open on my side" if window_note.startswith("Since")
            else f"{window_note}These times are open on my side")
    body = (f"Hi {_first(p)},\n\n{intro}\n\n{lead} (checked against my calendar just now):\n\n"
            f"{lines}\n\nTimes are shown in {ctx.settings.organizer_timezone} and UTC. I can't see your calendar, so "
            f"please reply with the option that suits you (for example \"Option B works\"), or suggest another time "
            f"with the date, time and your timezone." + _sign(ctx))
    p, ok = _send_reply(ctx, p, st, "propose_times", "propose_times", body)
    if not ok:
        return "escalated:send"
    store.save_prospect(ctx.conn, p, reply_status="SLOTS_PROPOSED")
    return "slots_proposed"


# ------------------------------------------------------------------ nodes

def build_graph(ctx, checkpointer):
    def classify(st: ReplyState) -> ReplyState:
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        if p["suppressed"]:
            return {"intent": "suppressed", "classification": {"reason": "prospect opted out; ignored"}}
        c = ctx.llm.structured("classify_reply", ctx.versions.system_prompt("classify_reply"), {
            "company": p["normalized_domain"], "meeting_status": p["meeting_status"],
            "times_proposed": [{"label": t["label"], "display": t["display"]} for t in p["times_proposed"]],
            "reply_body": st["body"][:6000]}, ReplyClassification)
        intent = c.intent
        if OPT_OUT_RE.search(st["body"]):
            intent = "opt_out"  # safety precedence regardless of model output
        store.log_activity(ctx.conn, p["id"], "classify_reply", intent, c.reason[:200], campaign_id=p["campaign_id"])
        return {"intent": intent, "classification": c.model_dump()}

    def route(st: ReplyState) -> str:
        return {
            "interested_meeting": "interest", "info_request": "info", "confirm_slot": "confirm",
            "counterproposal": "counter", "reject": "reject", "opt_out": "opt_out", "referral": "referral",
            "out_of_office": "ooo", "delivery_notice": "delivery_notice", "suppressed": "finalize",
        }.get(st["intent"], "handoff")

    def interest(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        return {"outcome": _propose(ctx, p, st, "Thanks for the reply — happy to set up a short call.",
                                    window=_window_for(ctx, st))}

    def info(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        ans = ctx.llm.structured("answer_info", ctx.versions.system_prompt("answer_info"), {
            "approved_capabilities": [{"id": c["id"], "approved_claim": c["approved_claim"]}
                                      for c in ctx.versions.capabilities["capabilities"]],
            "reply_body": st["body"][:4000]}, InfoAnswer)
        ids = [i for i in ans.capability_ids if i in ctx.versions.capability_ids()]
        if ans.unanswerable or not ids or len(ids) != len(ans.capability_ids):
            return {"outcome": _escalate(ctx, p, "UNSUPPORTED_REQUEST",
                                         f"Information request needs non-approved facts: {ans.reason[:200]}")}
        claims = "\n\n".join(ctx.versions.capability(i)["approved_claim"] for i in ids)
        body = (f"Hi {_first(p)},\n\nThanks for asking. In short:\n\n{claims}\n\n"
                f"If it would help to talk it through, just let me know." + _sign(ctx))
        p, ok = _send_reply(ctx, p, st, "info_answer", "info_answer", body)
        if ok:
            store.save_prospect(ctx.conn, p, reply_status="INFO_SENT")  # NOT meeting consent
        return {"outcome": "info_sent" if ok else "escalated:send"}

    def confirm(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        ref = (st["classification"].get("slot_reference") or "").strip().upper()
        options = p["times_proposed"]
        chosen = None
        if ref and len(ref) == 1:
            chosen = next((t for t in options if t["label"] == ref), None)
        elif not ref and len(options) == 1:
            chosen = options[0]
        if chosen is None:
            body = (f"Hi {_first(p)},\n\nThanks! To make sure I book the right time, which option should I use?\n\n" +
                    "\n".join(f"  Option {t['label']}: {t['display']}" for t in options) + _sign(ctx))
            if not options:
                return {"outcome": _escalate(ctx, p, "AMBIGUOUS_REPLY", "Confirmation received but no slot is outstanding")}
            p, ok = _send_reply(ctx, p, st, "clarify_slot", "clarify", body)
            if ok:
                store.save_prospect(ctx.conn, p, reply_status="CLARIFICATION_REQUESTED")
            return {"outcome": "clarification_requested"}
        slot = clock.parse(chosen["start"])
        if p["meeting_status"] in ("BOOKED", "RESCHEDULED") and p["confirmed_slot"] == chosen["start"]:
            return {"outcome": "already_booked"}
        # fresh availability check immediately before booking
        busy = _busy(ctx, p, slot, slot + timedelta(minutes=ctx.settings.meeting_minutes))
        p = store.get_prospect(ctx.conn, p["id"])
        if busy is None:
            return {"outcome": _escalate(ctx, p, "CALENDAR_UNAVAILABLE", "Fresh availability check failed; did not book")}
        own = p["calendar_event_id"]
        if own and p["confirmed_slot"]:  # our own existing event is not a conflict when rescheduling
            cs = clock.parse(p["confirmed_slot"])
            busy = [(s, e) for s, e in busy if s != cs]
        if not is_free(slot, ctx.settings.meeting_minutes, busy):
            p = store.save_prospect(ctx.conn, p, calendar_checked=1, calendar_checked_at=clock.iso(clock.now()),
                                    checked_slot=chosen["start"])
            return {"outcome": _propose(ctx, p, st, f"Sorry — {chosen['display']} was just taken on my side.",
                                        extra_exclude={chosen["start"]})}
        p = store.save_prospect(ctx.conn, p, calendar_checked=1, calendar_checked_at=clock.iso(clock.now()),
                                checked_slot=chosen["start"])
        if own and p["meeting_status"] in ("BOOKED", "RESCHEDULED"):
            res = run_tool(ctx, "update_meeting", {"prospect_id": p["id"], "op_id": f"update:{p['id']}:{chosen['start']}",
                                                   "event_id": own, "slot_start": chosen["start"]})
            new_status = "RESCHEDULED"
        else:
            res = run_tool(ctx, "book_meeting", {"prospect_id": p["id"], "op_id": f"book:{p['id']}:{chosen['start']}",
                                                 "slot_start": chosen["start"]})
            new_status = "BOOKED"
        p = store.get_prospect(ctx.conn, p["id"])
        if not res.ok:
            reason = "BOOKING_UNCERTAIN" if res.error_kind == ErrorKind.UNCERTAIN else "BOOKING_FAILED"
            return {"outcome": _escalate(ctx, p, reason, f"{res.error_kind}: {res.error}; no confirmation sent")}
        p = store.save_prospect(ctx.conn, p, confirmed_slot=chosen["start"], calendar_event_id=res.data["event_id"],
                                meeting_status=new_status, stage=Stage.MEETING_BOOKED, reply_status="MEETING_BOOKED")
        body = (f"Hi {_first(p)},\n\nConfirmed: {chosen['display']}. A calendar invitation has been sent." + _sign(ctx))
        p, ok = _send_reply(ctx, p, st, "booking_confirmation", "booking_confirmation", body)
        run_tool(ctx, "update_crm", {"prospect_id": p["id"], "outcome": f"MEETING_{new_status}",
                                     "note": f"{chosen['start']} event {res.data['event_id']}"})
        return {"outcome": f"meeting_{new_status.lower()}"}

    def counter(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        # idempotent on resume: only count the round once per incoming message
        rnd =p["reply_round"] + (0 if p.get("last_processed_message_id") == st["provider_message_id"] else 1)
        rejected = list(dict.fromkeys(p["rejected_slots"] + [t["start"] for t in p["times_proposed"]
                                                             if t.get("for_message") != st["provider_message_id"]]))
        p = store.save_prospect(ctx.conn, p, reply_round=rnd, rejected_slots=rejected, calendar_checked=0,
                                calendar_checked_at=None, checked_slot=None, requested_slot=None,
                                last_processed_message_id=st["provider_message_id"])
        if rnd > ctx.policy["reply"]["max_counterproposal_rounds"]:
            return {"outcome": _escalate(ctx, p, "COUNTERPROPOSAL_LIMIT",
                                         f"{rnd - 1} counterproposal rounds without confirmation")}
        c = st["classification"]
        pr = parse_proposed_time(c.get("proposed_time_text") or st["body"], c.get("timezone_text"),
                                 p["prospect_timezone"], clock.now())
        if not pr.ok:
            window = _window_for(ctx, st)
            if window:  # a general preference ("next week instead") -> offer verified slots inside it
                return {"outcome": _propose(ctx, p, st, "No problem.", window=window)}
            body = (f"Hi {_first(p)},\n\nThanks — I want to get this right: {pr.reason}. Could you share the exact date, "
                    f"time and your timezone (for example \"Wed 25 Nov, 16:00 Europe/London\")?" + _sign(ctx))
            p, ok = _send_reply(ctx, p, st, "clarify_time", "clarify", body)
            if ok:
                store.save_prospect(ctx.conn, p, reply_status="CLARIFICATION_REQUESTED")
            return {"outcome": "clarification_requested", "note": pr.reason}
        p = store.save_prospect(ctx.conn, p, prospect_timezone=pr.timezone, requested_slot=clock.iso(pr.start))
        ok_hours, why = within_working_hours(pr.start, ctx.settings)
        if not ok_hours:
            return {"outcome": _propose(ctx, p, st, f"Unfortunately {fmt_slot(pr.start, ctx.settings, pr.timezone)} {why}.",
                                        extra_exclude={clock.iso(pr.start)})}
        busy = _busy(ctx, p, pr.start, pr.start + timedelta(minutes=ctx.settings.meeting_minutes))
        p = store.get_prospect(ctx.conn, p["id"])
        if busy is None:
            return {"outcome": _escalate(ctx, p, "CALENDAR_UNAVAILABLE", "Availability check failed; nothing proposed")}
        p = store.save_prospect(ctx.conn, p, calendar_checked=1, calendar_checked_at=clock.iso(clock.now()),
                                checked_slot=clock.iso(pr.start))
        if not is_free(pr.start, ctx.settings.meeting_minutes, busy):
            return {"outcome": _propose(ctx, p, st, f"{fmt_slot(pr.start, ctx.settings, pr.timezone)} is not free on my side.",
                                        extra_exclude={clock.iso(pr.start)})}
        disp = fmt_slot(pr.start, ctx.settings, pr.timezone)
        p = store.save_prospect(ctx.conn, p, times_proposed=[{"label": "A", "start": clock.iso(pr.start), "display": disp,
                                                              "for_message": st["provider_message_id"]}])
        body = (f"Hi {_first(p)},\n\nThat time is open on my side: {disp}.\n\nPlease reply \"confirm\" and I'll send the "
                f"calendar invitation." + _sign(ctx))
        p, ok = _send_reply(ctx, p, st, "accept_counter", "propose_times", body)
        if ok:
            store.save_prospect(ctx.conn, p, reply_status="AWAITING_CONFIRMATION")
        return {"outcome": "awaiting_confirmation"}

    def reject(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        store.save_prospect(ctx.conn, p, stage=Stage.DONE, terminal_status="REJECTED", reply_status="REJECTED")
        run_tool(ctx, "update_crm", {"prospect_id": p["id"], "outcome": "REJECTED", "note": "prospect declined; outreach stopped"})
        return {"outcome": "rejected"}

    def opt_out(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        store.save_prospect(ctx.conn, p, suppressed=1, stage=Stage.DONE, terminal_status="OPTED_OUT", reply_status="OPTED_OUT")
        run_tool(ctx, "update_crm", {"prospect_id": p["id"], "outcome": "OPTED_OUT", "note": "suppressed from further outreach"})
        return {"outcome": "opted_out"}

    def referral(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        c = st["classification"]
        name, title = c.get("referral_name"), c.get("referral_title")
        entry = {"name": name, "title": title, "from_message": st["provider_message_id"], "at": clock.iso(clock.now())}
        cached = ctx.conn.execute("SELECT response FROM hunter_cache WHERE domain=?", (p["normalized_domain"],)).fetchone()
        match = None
        if name and cached:
            import json
            for cand in json.loads(cached["response"]).get("emails", []):
                full = f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip().lower()
                if full == name.strip().lower():
                    match = cand
        scored = None
        if match:
            need = {"compliance_evidence": p["qualification_detail"]["dimensions"]["compliance_workload"]["level"] >= 1,
                    "security_owner_found": True}
            scored = score_candidate(match, p["normalized_domain"], need, False, ctx.versions.rubric["contact"]["recent_source_months"])
        entry["identity_supported"] = bool(scored and scored["passed"])
        p = store.save_prospect(ctx.conn, p, referrals=p["referrals"] + [entry])
        if not entry["identity_supported"]:
            return {"outcome": _escalate(ctx, p, "REFERRAL_NEEDS_REVIEW",
                                         f"Referred to {name or 'someone'} ({title or 'title unknown'}); identity/contact rules not met")}
        scored.update({"via_referral": True, "selection_reason": f"referred by {p['contact']['name']}"})
        store.save_prospect(ctx.conn, p, contact=scored, contact_score=scored["total"], stage=Stage.CONTACT_SELECTED,
                            gmail_thread_id=None, times_proposed=[], reply_status="REFERRED", pain_hypotheses=[], draft=None)
        run_tool(ctx, "update_crm", {"prospect_id": p["id"], "outcome": "REFERRAL", "note": f"new contact {name}"})
        return {"outcome": "referral_accepted"}

    def ooo(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        store.save_prospect(ctx.conn, p, reply_status="OUT_OF_OFFICE_NOTICE")
        return {"outcome": "out_of_office_logged"}

    def delivery_notice(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        return {"outcome": _escalate(ctx, p, "DELIVERY_NOTICE", "Delivery/bounce notice received for the demo thread")}

    def handoff(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        reason = "UNSUPPORTED_REQUEST" if st["intent"] == "unsupported_request" else "AMBIGUOUS_REPLY"
        return {"outcome": _escalate(ctx, p, reason, (st["classification"].get("question_summary") or st["classification"].get("reason") or "")[:300])}

    def finalize(st):
        p = store.get_prospect(ctx.conn, st["prospect_id"])
        ctx.conn.execute("UPDATE messages SET processing_status='done', decision=? WHERE id=?",
                         (f"{st.get('intent')}: {st.get('outcome', 'ignored')}", st["message_db_id"]))
        store.save_prospect(ctx.conn, p, last_processed_message_id=st["provider_message_id"])
        return {}

    g = StateGraph(ReplyState)
    for name, fn in [("classify", classify), ("interest", interest), ("info", info), ("confirm", confirm),
                     ("counter", counter), ("reject", reject), ("opt_out", opt_out), ("referral", referral),
                     ("ooo", ooo), ("delivery_notice", delivery_notice), ("handoff", handoff), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route)
    for n in ("interest", "info", "confirm", "counter", "reject", "opt_out", "referral", "ooo", "delivery_notice", "handoff"):
        g.add_edge(n, "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def get_graph(ctx):
    if "reply_graph" not in ctx.extras:
        path = ctx.settings.checkpoint_db_path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        cconn = sqlite3.connect(path, check_same_thread=False)
        saver = SqliteSaver(cconn)
        ctx.extras["checkpoint_conn"] = cconn
        ctx.extras["reply_graph"] = build_graph(ctx, saver)
    return ctx.extras["reply_graph"]


def process_pending_messages(ctx, pid: str) -> list[str]:
    """Process this prospect's pending inbound messages in order. Pauses at escalation."""
    graph = get_graph(ctx)
    outcomes = []
    while True:
        p = store.get_prospect(ctx.conn, pid)
        if p["stage"] in (Stage.ESCALATED, Stage.WAITING_INTEGRATION):
            break
        row = ctx.conn.execute("SELECT * FROM messages WHERE prospect_id=? AND direction='inbound' AND "
                               "processing_status IN ('pending','processing') ORDER BY id LIMIT 1", (pid,)).fetchone()
        if row is None:
            break
        thread_id = f"reply:{pid}:{row['provider_message_id']}"
        config = {"configurable": {"thread_id": thread_id}}
        ctx.conn.execute("UPDATE messages SET processing_status='processing', graph_thread_id=? WHERE id=?", (thread_id, row["id"]))
        snapshot = graph.get_state(config)
        if snapshot.values and snapshot.next:
            result = graph.invoke(None, config)  # resume after crash from the last checkpoint
        elif snapshot.values and not snapshot.next:
            ctx.conn.execute("UPDATE messages SET processing_status='done' WHERE id=?", (row["id"],))
            continue
        else:
            result = graph.invoke({"prospect_id": pid, "message_db_id": row["id"],
                                   "provider_message_id": row["provider_message_id"], "body": row["body"] or ""}, config)
        outcomes.append(result.get("outcome", result.get("intent", "")))
    return outcomes
