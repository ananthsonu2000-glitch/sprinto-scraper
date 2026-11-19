"""Initial outreach pipeline (simple stage machine; state persisted after each stage).

QUEUED/RESEARCHING -> QUALIFIED -> CONTACT_SELECTED -> PAIN_READY -> DRAFT_READY -> AWAITING_REPLY
Any failure pauses only this prospect (escalation) — the batch continues.
"""
from __future__ import annotations

import logging
import traceback
from datetime import timedelta

from . import clock, store
from .contacts import select_contact
from .email_gen import draft_email
from .errors import ErrorKind, IntegrationError
from .pain import generate_pains
from .research.agent import research_prospect
from .store import Stage
from .tools import run_tool

log = logging.getLogger(__name__)
MAX_LLM_BUSY_TICKS = 3


def _initial_op_id(p: dict) -> str:
    op_id = f"outreach:{p['campaign_id']}:{p['normalized_domain']}"
    if (p.get("contact") or {}).get("via_referral"):
        op_id += ":referral:" + p["contact"]["name"].lower().replace(" ", "-")
    redrafts = int((p.get("tool_attempts") or {}).get("redrafts", 0))
    return op_id + (f":v{redrafts + 1}" if redrafts else "")


def send_initial(ctx, p: dict) -> dict:
    if p["suppressed"]:
        return store.save_prospect(ctx.conn, p, stage=Stage.DONE, terminal_status="OPTED_OUT")
    d = p["draft"]
    op_id = _initial_op_id(p)
    if ctx.settings.require_approval:
        from . import outbox
        outbox.queue(ctx, p, op_id, "initial_outreach", d["subject"], d["body"],
                     {"qa_score": (p.get("email_qa") or {}).get("score"), "claims": len(d.get("claims") or [])})
        return store.save_prospect(ctx.conn, store.get_prospect(ctx.conn, p["id"]), stage=Stage.AWAITING_APPROVAL)
    deliver_initial(ctx, p, op_id, d["subject"], d["body"])
    return store.get_prospect(ctx.conn, p["id"])


def deliver_initial(ctx, p: dict, op_id: str, subject: str, body: str) -> tuple[bool, str | None]:
    """Send the first email (directly, or after human approval) and move to AWAITING_REPLY."""
    if p["draft"] and (subject != p["draft"].get("subject") or body != p["draft"].get("body")):
        p = store.save_prospect(ctx.conn, p, draft={**p["draft"], "subject": subject, "body": body, "edited_by_human": True})
    res = run_tool(ctx, "send_email", {"prospect_id": p["id"], "op_id": op_id, "purpose": "initial_outreach",
                                       "subject": subject, "body": body}, reason="initial outreach after QA pass")
    p = store.get_prospect(ctx.conn, p["id"])
    if not res.ok:
        if res.error_kind == ErrorKind.UNCERTAIN:
            store.open_escalation(ctx.conn, p, "SEND_UNCERTAIN", f"initial outreach {op_id}: {res.error}")
        elif res.error_kind in (ErrorKind.SETUP_REQUIRED, ErrorKind.AUTH):
            store.set_integration_status(ctx.conn, "gmail", "setup_required" if res.error_kind == ErrorKind.SETUP_REQUIRED else "auth_failed", res.error or "")
            store.open_escalation(ctx.conn, p, "SETUP_REQUIRED", f"Email delivery unavailable: {res.error}")
        else:
            store.open_escalation(ctx.conn, p, "SEND_FAILED", f"{res.error_kind}: {res.error}")
        return False, f"{res.error_kind}: {res.error}"
    deadline = clock.now() + timedelta(hours=ctx.settings.no_reply_handoff_hours)
    p = store.save_prospect(ctx.conn, p, gmail_thread_id=res.data["thread_id"],
                            outbound_message_ids=list(dict.fromkeys(p["outbound_message_ids"] + [res.data["provider_message_id"]])),
                            stage=Stage.AWAITING_REPLY, reply_status="AWAITING_REPLY",
                            reply_deadline=clock.iso(deadline))
    run_tool(ctx, "update_crm", {"prospect_id": p["id"], "outcome": "OUTREACH_SENT",
                                 "note": f"{res.data['delivery_label']} to demo address; thread {res.data['thread_id']}"})
    store.save_prospect(ctx.conn, store.get_prospect(ctx.conn, p["id"]), crm_logged=1)
    return True, None


STAGE_HANDLERS = {
    Stage.QUEUED: research_prospect,
    Stage.RESEARCHING: research_prospect,
    Stage.QUALIFIED: select_contact,
    Stage.CONTACT_SELECTED: generate_pains,
    Stage.PAIN_READY: draft_email,
    Stage.DRAFT_READY: send_initial,
}


def process_prospect(ctx, pid: str, max_steps: int = 10) -> dict:
    """Advance one prospect as far as possible. Never raises (batch isolation)."""
    p = store.get_prospect(ctx.conn, pid)
    for _ in range(max_steps):
        if p is None or p["terminal_status"] or p["stage"] not in STAGE_HANDLERS:
            break
        handler = STAGE_HANDLERS[p["stage"]]
        before = (p["stage"], p["version"])
        try:
            p = handler(ctx, p)
        except store.StaleStateError as e:
            store.log_activity(ctx.conn, pid, "stage", "stale_state", str(e))
            break
        except IntegrationError as e:
            p = store.get_prospect(ctx.conn, pid)
            if e.kind in (ErrorKind.SETUP_REQUIRED, ErrorKind.AUTH):
                store.set_integration_status(ctx.conn, e.integration or "unknown",
                                             "setup_required" if e.kind == ErrorKind.SETUP_REQUIRED else "auth_failed", e.message)
                store.save_prospect(ctx.conn, p, stage=Stage.WAITING_INTEGRATION, paused_stage=p["stage"],
                                    last_error=f"{e.integration}: {e.message}")
                store.log_activity(ctx.conn, pid, "stage", "paused", f"{e.integration} {e.kind}: {e.message}",
                                   campaign_id=p["campaign_id"])
            elif e.kind == ErrorKind.QUOTA and e.integration == "llm":
                # Free-tier daily quota used up: expected, not a failure. Keep the stage; the worker
                # resumes automatically after the reset (no API calls are made while on hold).
                ctx.conn.execute("UPDATE prospects SET last_error=? WHERE id=?", (f"waiting for free AI quota: {e.message[:300]}", pid))
                store.log_activity(ctx.conn, pid, "stage", "waiting_for_quota", e.message[:300], campaign_id=p["campaign_id"])
            elif e.kind == ErrorKind.TRANSIENT and e.integration == "llm":
                # Every model was busy/overloaded right now: keep the stage and retry on later
                # ticks; only hand off to a human after repeated failures.
                tries = int((p.get("tool_attempts") or {}).get("llm_busy_ticks", 0)) + 1
                ctx.conn.execute("UPDATE prospects SET tool_attempts=json_set(tool_attempts,'$.llm_busy_ticks',?), "
                                 "last_error=? WHERE id=?", (tries, f"llm busy (try {tries}): {e.message[:400]}", pid))
                if tries >= MAX_LLM_BUSY_TICKS:
                    store.open_escalation(ctx.conn, store.get_prospect(ctx.conn, pid), "LLM_UNAVAILABLE",
                                          f"All AI models failed on {tries} worker ticks: {e.message[:300]}")
                else:
                    store.log_activity(ctx.conn, pid, "stage", "retry_later",
                                       f"all AI models busy (attempt {tries}/{MAX_LLM_BUSY_TICKS}); will retry next tick",
                                       campaign_id=p["campaign_id"])
            else:
                store.open_escalation(ctx.conn, p, "INTEGRATION_ERROR", f"{p['stage']}: {e}")
            break
        except Exception as e:  # noqa: BLE001 - isolate unexpected failures to this prospect
            log.exception("prospect %s failed", pid)
            p = store.get_prospect(ctx.conn, pid)
            store.save_prospect(ctx.conn, p, last_error=traceback.format_exc()[-1500:])
            store.open_escalation(ctx.conn, store.get_prospect(ctx.conn, pid), "UNEXPECTED_ERROR", f"{type(e).__name__}: {e}")
            break
        p = store.get_prospect(ctx.conn, pid)
        if (p["stage"], p["version"]) == before:
            break
    return store.get_prospect(ctx.conn, pid)


def create_campaign(ctx, name: str, domains: list[str]) -> dict:
    from .research.url_guard import normalize_domains
    norm, rejected = normalize_domains(domains)
    v = ctx.versions
    cid = store.new_id("cmp")
    now = clock.iso(clock.now())
    ctx.conn.execute("INSERT INTO campaigns(id,name,mode,rubric_version,prompt_version,policy_version,capability_version,created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)", (cid, name, ctx.settings.mode, v.rubric_version, v.prompt_version,
                                                   v.policy_version, v.capability_version, now))
    ids = []
    for d in norm:
        pid = store.new_id("pr")
        ctx.conn.execute(
            "INSERT INTO prospects(id,campaign_id,normalized_domain,mode,stage,rubric_version,prompt_version,policy_version,"
            "capability_version,organizer_timezone,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, cid, d, ctx.settings.mode, Stage.QUEUED, v.rubric_version, v.prompt_version, v.policy_version,
             v.capability_version, ctx.settings.organizer_timezone, now, now))
        ids.append(pid)
    store.log_activity(ctx.conn, None, "campaign_created", "ok",
                       f"{len(norm)} domains queued; {len(rejected)} rejected", details={"rejected": rejected},
                       actor="human", campaign_id=cid)
    return {"campaign_id": cid, "prospect_ids": ids, "domains": norm, "rejected": rejected}
