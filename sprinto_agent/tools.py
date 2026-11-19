"""Explicit, typed tools. Every side effect in the system goes through one of these.

Each ToolSpec documents: what it does, when to use it, when NOT to use it, and what
it returns. Inputs are pydantic-validated; outputs are a structured ToolResult
(success data or a classified error). Every call is written to the Demo activity log.

Note: no tool accepts a recipient or attendee. Delivery addresses come only from
settings and are re-verified inside each integration (delivery_guard.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ValidationError

from . import clock, store
from .errors import DeliveryPolicyViolation, ErrorKind, IntegrationError, OperationUncertain
from .integrations.delivery_guard import demo_event_summary, demo_subject
from .integrations.mailbox import OutboundEmail
from .ops import Ledger
from .retry import with_retries


@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error_kind: str | None = None
    error: str | None = None


# ------------------------------------------------------------------ inputs
class FetchPageIn(BaseModel):
    prospect_id: str
    url: str
    research_round: int = 0
    selection_reason: str = ""


class HunterIn(BaseModel):
    prospect_id: str
    domain: str


class SendEmailIn(BaseModel):
    prospect_id: str
    op_id: str
    purpose: Literal["initial_outreach", "propose_times", "clarify", "info_answer", "booking_confirmation", "acknowledge", "follow_up"]
    subject: str
    body: str
    in_reply_to_message_id: Optional[str] = None
    # deliberately NO recipient fields


class ReadThreadIn(BaseModel):
    prospect_id: str


class CheckCalendarIn(BaseModel):
    prospect_id: str
    window_start: str
    window_end: str


class BookMeetingIn(BaseModel):
    prospect_id: str
    op_id: str
    slot_start: str


class UpdateMeetingIn(BaseModel):
    prospect_id: str
    op_id: str
    event_id: str
    slot_start: str


class UpdateCrmIn(BaseModel):
    prospect_id: str
    outcome: str
    note: str = ""


class EscalateIn(BaseModel):
    prospect_id: str
    reason: str
    details: str


@dataclass
class ToolSpec:
    name: str
    what: str
    when: str
    when_not: str
    returns: str
    input_model: type[BaseModel]
    impl: Callable[[Any, BaseModel], ToolResult]
    reversible: bool = True


# ------------------------------------------------------------------ implementations

def _p(ctx, pid):
    p = store.get_prospect(ctx.conn, pid)
    if p is None:
        raise IntegrationError(ErrorKind.INVALID_INPUT, f"unknown prospect {pid}", "tool")
    return p


def _fetch_website_page(ctx, a: FetchPageIn) -> ToolResult:
    from .research.url_guard import canonical_url
    a.url = canonical_url(a.url)
    conn = ctx.conn
    existing = conn.execute("SELECT * FROM pages WHERE prospect_id=? AND url=?", (a.prospect_id, a.url)).fetchone()
    if existing:
        return ToolResult(True, {"page_id": existing["id"], "status": existing["status"], "cached": True})
    n = conn.execute("SELECT COUNT(*) FROM pages WHERE prospect_id=?", (a.prospect_id,)).fetchone()[0]
    if n >= ctx.policy["research"]["max_pages_per_domain"]:
        return ToolResult(False, error_kind=ErrorKind.INVALID_INPUT, error="per-domain page budget exhausted")
    attempts: list[int] = []
    try:
        r = with_retries(lambda: ctx.fetcher.fetch(a.url), ctx, attempts_out=attempts)
        status, final, http, title, text, links, err, method, ts = (
            r.status, r.final_url, r.http_status, r.title, r.text, r.links, r.error, r.method, r.retrieved_at)
    except IntegrationError as e:
        status, final, http, title, text, links, err, method, ts = (
            "failed", None, None, None, "", [], f"{e.kind}: {e.message} after {len(attempts)} attempts", "http",
            clock.iso(clock.now()))
    pid = store.new_id("pg")
    conn.execute(
        "INSERT INTO pages(id,prospect_id,url,final_url,status,fetch_method,http_status,title,text,links,error,research_round,selection_reason,retrieved_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, a.prospect_id, a.url, final, status, method, http, title, text, json.dumps(links), err,
         a.research_round, a.selection_reason, ts))
    data = {"page_id": pid, "status": status, "final_url": final, "title": title, "text_chars": len(text or ""),
            "links": len(links), "attempts": len(attempts), "error": err}
    if status != "ok":
        return ToolResult(False, data, error_kind="retrieval_failure", error=err)
    return ToolResult(True, data)


def _search_hunter(ctx, a: HunterIn) -> ToolResult:
    conn = ctx.conn
    st = store.get_integration_status(conn, "hunter")
    if st and st["status"] in ("paused_quota", "auth_failed"):
        return ToolResult(False, error_kind=ErrorKind.QUOTA if st["status"] == "paused_quota" else ErrorKind.AUTH,
                          error=f"Hunter paused: {st['detail']}")
    ttl = timedelta(days=ctx.policy["hunter"]["cache_ttl_days"])
    row = conn.execute("SELECT * FROM hunter_cache WHERE domain=?", (a.domain,)).fetchone()
    if row and clock.parse(row["fetched_at"]) > clock.now() - ttl:
        return ToolResult(True, {**json.loads(row["response"]), "cached": True})
    try:
        data = with_retries(lambda: ctx.hunter.domain_search(a.domain), ctx)
    except IntegrationError as e:
        if e.kind == ErrorKind.QUOTA:
            store.set_integration_status(conn, "hunter", "paused_quota", e.message)
        elif e.kind == ErrorKind.AUTH:
            store.set_integration_status(conn, "hunter", "auth_failed", e.message)
        elif e.kind == ErrorKind.SETUP_REQUIRED:
            store.set_integration_status(conn, "hunter", "setup_required", e.message)
        return ToolResult(False, error_kind=e.kind, error=e.message)
    conn.execute("INSERT INTO hunter_cache(domain,response,fetched_at) VALUES (?,?,?) ON CONFLICT(domain) DO UPDATE"
                 " SET response=excluded.response, fetched_at=excluded.fetched_at",
                 (a.domain, json.dumps(data), clock.iso(clock.now())))
    return ToolResult(True, {**data, "cached": False})


def _send_email(ctx, a: SendEmailIn) -> ToolResult:
    p = _p(ctx, a.prospect_id)
    company = (p["company_profile"].get("company_name") or {}).get("value") or p["normalized_domain"]
    if isinstance(company, list):
        company = company[0]
    subject = demo_subject(company, a.subject)
    from .email_layout import ensure_layout
    a.body = ensure_layout(a.body)  # never send a wall of text
    msg = OutboundEmail(to=[ctx.settings.delivery_address], subject=subject, body=a.body, op_id=a.op_id,
                        thread_id=p.get("gmail_thread_id"), in_reply_to=a.in_reply_to_message_id)
    ledger = Ledger(ctx.conn, ctx.policy["retries"]["send_reconcile_retry_max"])

    def do():
        ref = ctx.mailbox.send(msg)
        return json.dumps({"id": ref.provider_message_id, "thread_id": ref.thread_id})

    def reconcile():
        ref = ctx.mailbox.find_sent(msg.message_id_header)
        return json.dumps({"id": ref.provider_message_id, "thread_id": ref.thread_id}) if ref else None

    try:
        ref_json, executed = ledger.run(a.op_id, "send_email", a.prospect_id,
                                        {"purpose": a.purpose, "subject": subject}, do, reconcile)
    except DeliveryPolicyViolation as e:
        ledger.mark(a.op_id, "failed_permanent", error=f"policy: {e}")
        return ToolResult(False, error_kind="policy_violation", error=str(e))
    except OperationUncertain as e:
        return ToolResult(False, {"op_id": a.op_id}, error_kind=ErrorKind.UNCERTAIN, error=str(e))
    except IntegrationError as e:
        return ToolResult(False, {"op_id": a.op_id}, error_kind=e.kind, error=e.message)
    ref = json.loads(ref_json)
    store.record_message(ctx.conn, a.prospect_id, ref["id"], ref["thread_id"], "outbound", "agent", subject, a.body,
                         delivery_label=getattr(ctx.mailbox, "delivery_label", ""))
    return ToolResult(True, {"provider_message_id": ref["id"], "thread_id": ref["thread_id"], "subject": subject,
                             "executed_now": executed, "delivery_label": getattr(ctx.mailbox, "delivery_label", ""),
                             "message_id_header": msg.message_id_header})


def _read_thread_replies(ctx, a: ReadThreadIn) -> ToolResult:
    p = _p(ctx, a.prospect_id)
    if not p.get("gmail_thread_id"):
        return ToolResult(False, error_kind=ErrorKind.INVALID_INPUT, error="no tracked thread")
    try:
        msgs = with_retries(lambda: ctx.mailbox.list_thread(p["gmail_thread_id"]), ctx)
    except IntegrationError as e:
        return ToolResult(False, error_kind=e.kind, error=e.message)
    outbound = set(p["outbound_message_ids"])
    new = 0
    ignored_agent = 0
    for m in msgs:
        if m.provider_message_id in outbound or m.is_agent or m.author_kind == "agent":
            ignored_agent += 1  # feedback-loop prevention: never treat our own mail as a reply
            continue
        kind = "simulated_prospect" if m.author_kind == "simulated_prospect" else "user"
        if store.record_message(ctx.conn, p["id"], m.provider_message_id, p["gmail_thread_id"], "inbound", kind,
                                m.subject, m.body, sent_at=m.sent_at, processing_status="pending"):
            new += 1
    return ToolResult(True, {"new_replies": new, "ignored_agent_messages": ignored_agent})


def _check_calendar(ctx, a: CheckCalendarIn) -> ToolResult:
    start, end = clock.parse(a.window_start), clock.parse(a.window_end)
    try:
        busy = with_retries(lambda: ctx.calendar.freebusy(start, end), ctx,
                            max_attempts=ctx.policy["retries"]["calendar_check_max_attempts"])
    except IntegrationError as e:
        return ToolResult(False, error_kind=e.kind, error=e.message)
    return ToolResult(True, {"busy": [(clock.iso(s), clock.iso(e)) for s, e in busy],
                             "checked_at": clock.iso(clock.now())})


def _meeting_meta(ctx, p, slot_start: str):
    start = clock.parse(slot_start)
    end = start + timedelta(minutes=ctx.settings.meeting_minutes)
    company = (p["company_profile"].get("company_name") or {}).get("value") or p["normalized_domain"]
    contact = (p.get("contact") or {}).get("name", "prospect")
    summary = demo_event_summary(f"Sprinto intro — {company} ({contact})")
    return start, end, summary


def _book_meeting(ctx, a: BookMeetingIn) -> ToolResult:
    p = _p(ctx, a.prospect_id)
    start, end, summary = _meeting_meta(ctx, p, a.slot_start)
    ledger = Ledger(ctx.conn, ctx.policy["retries"]["send_reconcile_retry_max"])
    attendees = [ctx.settings.delivery_address]
    try:
        eid, executed = ledger.run(
            a.op_id, "book_meeting", a.prospect_id, {"slot_start": a.slot_start, "summary": summary},
            lambda: ctx.calendar.create_event(a.op_id, summary, start, end, attendees,
                                              "Demo booking created by the Sprinto prospecting MVP."),
            lambda: ctx.calendar.find_event_by_op(a.op_id))
    except DeliveryPolicyViolation as e:
        return ToolResult(False, error_kind="policy_violation", error=str(e))
    except OperationUncertain as e:
        return ToolResult(False, {"op_id": a.op_id}, error_kind=ErrorKind.UNCERTAIN, error=str(e))
    except IntegrationError as e:
        return ToolResult(False, {"op_id": a.op_id}, error_kind=e.kind, error=e.message)
    return ToolResult(True, {"event_id": eid, "executed_now": executed, "summary": summary,
                             "label": getattr(ctx.calendar, "event_label", "REAL")})


def _update_meeting(ctx, a: UpdateMeetingIn) -> ToolResult:
    p = _p(ctx, a.prospect_id)
    start, end, summary = _meeting_meta(ctx, p, a.slot_start)
    ledger = Ledger(ctx.conn, ctx.policy["retries"]["send_reconcile_retry_max"])
    attendees = [ctx.settings.delivery_address]
    try:
        eid, executed = ledger.run(
            a.op_id, "update_meeting", a.prospect_id, {"event_id": a.event_id, "slot_start": a.slot_start},
            lambda: ctx.calendar.update_event(a.event_id, a.op_id, summary, start, end, attendees),
            lambda: ctx.calendar.find_update_by_op(a.op_id))
    except OperationUncertain as e:
        return ToolResult(False, {"op_id": a.op_id}, error_kind=ErrorKind.UNCERTAIN, error=str(e))
    except (IntegrationError, DeliveryPolicyViolation) as e:
        return ToolResult(False, error_kind=getattr(e, "kind", "policy_violation"), error=str(e))
    return ToolResult(True, {"event_id": eid, "executed_now": executed})


def _update_crm(ctx, a: UpdateCrmIn) -> ToolResult:
    p = _p(ctx, a.prospect_id)
    ok = store.log_activity(ctx.conn, a.prospect_id, "crm_update", "logged", f"{a.outcome}: {a.note}",
                            tool="update_crm", details={"target": "Demo activity log", "outcome": a.outcome},
                            campaign_id=p["campaign_id"])
    if not ok:
        return ToolResult(False, error_kind=ErrorKind.TRANSIENT, error="activity log write failed (queued for replay)")
    return ToolResult(True, {"target": "Demo activity log"})


def _escalate(ctx, a: EscalateIn) -> ToolResult:
    p = _p(ctx, a.prospect_id)
    esc = store.open_escalation(ctx.conn, p, a.reason, a.details)
    return ToolResult(True, esc)


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("fetch_website_page",
             "Fetches one public web page on the prospect's site through the SSRF guard (every redirect re-validated), extracts title/text/links, and stores it.",
             "Initial research (homepage + up to 3 relevant pages) and research-agent rounds (max 2 new pages per round) when a specific qualification evidence gap exists.",
             "Never for non-public, login-protected, or off-site URLs; never to re-fetch an already stored page; not when the per-domain page budget is exhausted.",
             "{page_id, status, final_url, title, text_chars, links, attempts, error}. Blocked/failed pages are retrieval failures, not negative evidence.",
             FetchPageIn, _fetch_website_page),
    ToolSpec("search_hunter_contacts",
             "Calls Hunter Domain Search for the company domain (cached; quota-aware).",
             "Once per account after qualification passes, to obtain candidate named contacts.",
             "Not before qualification; not when Hunter is paused for quota/auth; never to obtain delivery addresses (prospect emails are research data only).",
             "Hunter `data` object with `emails` candidates and `cached` flag, or a quota/auth/setup error.",
             HunterIn, _search_hunter),
    ToolSpec("send_email",
             "Sends an email in the tracked thread. Recipient is ALWAYS the configured demo address (not an input). Idempotent via operation ID; uncertain outcomes are reconciled by Message-ID.",
             "Initial outreach after QA passes; replies: proposing verified slots, clarifying, approved-information answers, booking confirmation after the event exists.",
             "Never to a prospect address; never before QA; never a booking confirmation before calendar creation succeeds; never as an automatic chase.",
             "{provider_message_id, thread_id, subject, executed_now, delivery_label} or an error (uncertain/auth/policy_violation/...).",
             SendEmailIn, _send_email, reversible=False),
    ToolSpec("read_thread_replies",
             "Reads the tracked campaign thread and ingests new user-authored messages (deduped by provider message ID).",
             "During polling for prospects with a tracked thread.",
             "Never for unrelated inbox content; agent-authored messages (our outbound IDs or X-Sprinto-Agent header) are ignored to prevent reply loops.",
             "{new_replies, ignored_agent_messages}.",
             ReadThreadIn, _read_thread_replies),
    ToolSpec("check_calendar",
             "Reads the ORGANIZER's free/busy for a window (with bounded retries).",
             "Before proposing slots and immediately before any booking (fresh check).",
             "Never to infer the prospect's availability; never reuse a stale check after a counterproposal.",
             "{busy: [[start,end],...], checked_at} or an error (on failure no slots may be proposed).",
             CheckCalendarIn, _check_calendar),
    ToolSpec("book_meeting",
             "Creates a [DEMO] calendar event for an explicitly agreed exact slot; attendee is only the demo address. Idempotent by conversation+slot op ID.",
             "Only after the prospect explicitly confirms an exact slot AND a fresh availability check shows it free.",
             "Not on interest alone; not on ambiguous confirmations; not for an already-booked meeting (use update_meeting).",
             "{event_id, executed_now, summary, label} or an error (uncertain -> BOOKING_UNCERTAIN escalation).",
             BookMeetingIn, _book_meeting, reversible=False),
    ToolSpec("update_meeting",
             "Moves the existing calendar event to a newly agreed slot (no duplicate event).",
             "When a confirmed meeting must change and the new exact slot is explicitly agreed and freshly checked.",
             "Not to create a meeting; not without explicit agreement.",
             "{event_id, executed_now} or an error.",
             UpdateMeetingIn, _update_meeting, reversible=False),
    ToolSpec("update_crm",
             "Records an outcome in the internal Demo activity log (adapter boundary for a future external CRM).",
             "After terminal outcomes and key conversation events.",
             "Do not imply an external CRM is connected.",
             "{target: 'Demo activity log'} or a transient error (the action itself is never undone).",
             UpdateCrmIn, _update_crm),
    ToolSpec("escalate",
             "Pauses ONLY this prospect and opens a human-review item with a reason and allowed resume actions.",
             "Ambiguity, unsupported requests, uncertain side effects, retry/silence limits, setup failures.",
             "Never invent a decision to avoid a handoff; do not pause the whole batch.",
             "{id, reason}.",
             EscalateIn, _escalate),
]}


def run_tool(ctx, name: str, args: dict, reason: str = "") -> ToolResult:
    spec = TOOLS[name]
    try:
        inp = spec.input_model(**args)
    except ValidationError as e:
        res = ToolResult(False, error_kind=ErrorKind.INVALID_INPUT, error=str(e)[:300])
        store.log_activity(ctx.conn, args.get("prospect_id"), name, "invalid_input", res.error, tool=name)
        return res
    try:
        res = spec.impl(ctx, inp)
    except IntegrationError as e:
        res = ToolResult(False, error_kind=e.kind, error=e.message)
    pid = args.get("prospect_id")
    p = store.get_prospect(ctx.conn, pid) if pid else None
    if p is not None:
        # Counter only: updated in place without bumping the optimistic-lock version,
        # so callers holding the prospect are not invalidated by bookkeeping.
        ctx.conn.execute(
            "UPDATE prospects SET tool_attempts=json_set(tool_attempts, ?, COALESCE(json_extract(tool_attempts, ?), 0) + 1)"
            " WHERE id=?", (f"$.{name}", f"$.{name}", pid))
    if name not in ("update_crm", "escalate"):
        summary = {k: v for k, v in res.data.items() if k not in ("emails", "links", "busy")}
        store.log_activity(ctx.conn, pid, name, "ok" if res.ok else f"error:{res.error_kind}",
                           reason or (res.error or ""), tool=name, details=summary,
                           campaign_id=p["campaign_id"] if p else None)
    return res


def tool_catalog() -> list[dict]:
    return [{"name": t.name, "what": t.what, "when": t.when, "when_not": t.when_not, "returns": t.returns,
             "reversible": t.reversible, "input_schema": t.input_model.model_json_schema()} for t in TOOLS.values()]
