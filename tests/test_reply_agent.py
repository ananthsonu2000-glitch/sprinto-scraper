"""LangGraph reply & scheduling agent behaviour."""
from datetime import timedelta

from helpers import acme_awaiting, agent_mails, open_reasons, reply

from sprinto_agent import clock, store
from sprinto_agent.worker import Worker


def test_interest_proposes_verified_slots_with_explicit_timezones(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Thanks, I'm interested — happy to chat.")
    assert p["reply_status"] == "SLOTS_PROPOSED" and p["calendar_checked"] == 1
    assert len(p["times_proposed"]) == 3
    body = agent_mails(ctx)[-1]["body"]
    for t in p["times_proposed"]:
        assert t["display"] in body and "Asia/Kolkata" in t["display"] and "UTC" in t["display"]
    assert p["meeting_status"] is None  # interest alone is not permission to book


def test_busy_slots_are_never_proposed(ctx):
    p = acme_awaiting(ctx)
    # block 11:00 IST tomorrow (05:30Z)
    start = clock.parse("2026-11-18T05:30:00+00:00")
    ctx.calendar.add_busy(start, start + timedelta(minutes=30))
    p = reply(ctx, p, "Interested, let's talk.")
    assert "2026-11-18T05:30:00+00:00" not in [t["start"] for t in p["times_proposed"]]


def test_send_information_is_not_meeting_consent(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Can you send me more info about how evidence collection works?")
    assert p["reply_status"] == "INFO_SENT"
    assert p["times_proposed"] == [] and p["meeting_status"] is None
    assert ctx.conn.execute("SELECT COUNT(*) FROM sim_calendar_events").fetchone()[0] == 0
    approved = ctx.versions.capability("evidence_collection")["approved_claim"]
    assert approved in agent_mails(ctx)[-1]["body"]


def test_pricing_question_hands_off(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "What does it cost? Can you send pricing?")
    assert p["stage"] == "ESCALATED" and "UNSUPPORTED_REQUEST" in open_reasons(ctx, p["id"])
    assert len(agent_mails(ctx)) == 1  # no answer invented


def test_booking_requires_explicit_agreement_and_fresh_check(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    calls = ctx.calendar.freebusy_calls
    p = reply(ctx, p, "Sounds good")  # not an exact slot among three options
    assert p["meeting_status"] is None and p["reply_status"] == "CLARIFICATION_REQUESTED"
    p = reply(ctx, p, "Option B works for me")
    assert ctx.calendar.freebusy_calls == calls + 1  # fresh availability check right before booking
    assert p["meeting_status"] == "BOOKED" and p["confirmed_slot"] == p["times_proposed"][1]["start"]
    assert agent_mails(ctx)[-1]["body"].startswith(f"Hi Priya,\n\nConfirmed: {p['times_proposed'][1]['display']}")


def test_slot_taken_before_booking_proposes_alternatives(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    b = p["times_proposed"][1]
    s = clock.parse(b["start"])
    ctx.calendar.add_busy(s, s + timedelta(minutes=30), "someone else booked it")
    p = reply(ctx, p, "Option B works")
    assert p["meeting_status"] is None
    assert b["start"] not in [t["start"] for t in p["times_proposed"]]
    assert "just taken" in agent_mails(ctx)[-1]["body"]


def test_failed_booking_never_sends_confirmation(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    ctx.calendar.fail_create = ["permanent"]
    n = len(agent_mails(ctx))
    p = reply(ctx, p, "Option A works")
    assert p["meeting_status"] is None and p["stage"] == "ESCALATED"
    assert "BOOKING_FAILED" in open_reasons(ctx, p["id"])
    assert len(agent_mails(ctx)) == n
    assert all("Confirmed" not in m["body"] for m in agent_mails(ctx))


def test_counterproposal_invalidates_stale_availability(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    old = [t["start"] for t in p["times_proposed"]]
    checked_at = p["calendar_checked_at"]
    clock.set_now(clock.now() + timedelta(minutes=5))
    calls = ctx.calendar.freebusy_calls
    p = reply(ctx, p, "None of those work. Could we do November 24 at 4pm Asia/Kolkata instead?")
    assert p["reply_round"] == 1
    assert set(old) <= set(p["rejected_slots"])
    assert ctx.calendar.freebusy_calls == calls + 1 and p["calendar_checked_at"] != checked_at
    assert p["requested_slot"] == "2026-11-24T10:30:00+00:00" and p["prospect_timezone"] == "Asia/Kolkata"
    assert p["reply_status"] == "AWAITING_CONFIRMATION" and p["meeting_status"] is None
    p = reply(ctx, p, "Confirm")
    assert p["meeting_status"] == "BOOKED" and p["confirmed_slot"] == "2026-11-24T10:30:00+00:00"


def test_ambiguous_timezone_requests_clarification(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    p = reply(ctx, p, "How about November 25 at 3pm instead?")
    assert p["reply_status"] == "CLARIFICATION_REQUESTED" and p["requested_slot"] is None
    assert "timezone" in agent_mails(ctx)[-1]["body"]
    p = reply(ctx, p, "How about November 25 at 3pm IST instead?")  # IST is ambiguous
    assert p["reply_status"] == "CLARIFICATION_REQUESTED" and p["meeting_status"] is None


def test_counterproposal_limit_hands_off(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    p = reply(ctx, p, "Could we do November 24 at 2am UTC instead?")   # round 1 (outside hours)
    p = reply(ctx, p, "Could we do November 25 at 2am UTC instead?")   # round 2
    p = reply(ctx, p, "Could we do November 26 at 2am UTC instead?")   # round 3 -> handoff
    assert p["stage"] == "ESCALATED" and "COUNTERPROPOSAL_LIMIT" in open_reasons(ctx, p["id"])


def test_reschedule_updates_existing_event(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    p = reply(ctx, p, "Option A works")
    eid = p["calendar_event_id"]
    p = reply(ctx, p, "Something came up. Could we do November 24 at 4pm Asia/Kolkata instead?")
    p = reply(ctx, p, "Confirm")
    assert p["meeting_status"] == "RESCHEDULED" and p["calendar_event_id"] == eid
    rows = ctx.conn.execute("SELECT * FROM sim_calendar_events").fetchall()
    assert len(rows) == 1 and rows[0]["start"] == "2026-11-24T10:30:00+00:00"


def test_reject_stops_and_opt_out_suppresses(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Not interested, thanks.")
    assert p["terminal_status"] == "REJECTED"
    n = len(agent_mails(ctx))
    reply(ctx, p, "Actually, interested — happy to chat.")
    assert len(agent_mails(ctx)) == n  # terminal: no more outreach


def test_opt_out_prevents_further_outreach(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Please remove me from your list.")
    assert p["suppressed"] == 1 and p["terminal_status"] == "OPTED_OUT"
    n = len(agent_mails(ctx))
    ctx.mailbox.inject_prospect_reply(p["gmail_thread_id"], "Interested, let's talk")
    msgs_before = ctx.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    Worker(ctx).tick()
    assert len(agent_mails(ctx)) == n
    assert ctx.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == msgs_before  # thread no longer polled


def test_out_of_office_is_not_interest(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "I am out of the office until Monday with limited access to email.")
    assert p["reply_status"] == "OUT_OF_OFFICE_NOTICE" and p["times_proposed"] == []


def test_referral_without_supported_identity_hands_off(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Please talk to Jane Doe, our security lead.")
    assert p["stage"] == "ESCALATED" and "REFERRAL_NEEDS_REVIEW" in open_reasons(ctx, p["id"])
    assert p["referrals"][0]["name"] == "Jane Doe"


def test_referral_with_supported_identity_proceeds(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Please talk to Daniel Ortiz, our CISO.")
    assert p["contact"]["name"] == "Daniel Ortiz" and p["contact"]["via_referral"]
    Worker(ctx).tick()  # new outreach to the referred contact (still delivered to the demo address)
    mails = agent_mails(ctx)
    assert len(mails) == 2
    assert mails[-1]["body"].startswith("Hi Daniel") and mails[-1]["to_addr"] == "demo.inbox@example.com"


def test_no_reply_deadline_hands_off_without_chasing(ctx):
    p = acme_awaiting(ctx)
    n = len(agent_mails(ctx))
    clock.set_now(clock.now() + timedelta(hours=47))
    Worker(ctx).tick()
    assert store.get_prospect(ctx.conn, p["id"])["stage"] == "AWAITING_REPLY"
    clock.set_now(clock.now() + timedelta(hours=2))
    Worker(ctx).tick()
    p = store.get_prospect(ctx.conn, p["id"])
    assert p["stage"] == "ESCALATED" and "NO_REPLY" in open_reasons(ctx, p["id"])
    assert len(agent_mails(ctx)) == n
    assert not ctx.conn.execute("SELECT 1 FROM messages WHERE direction='inbound'").fetchone()  # no invented reply


def test_calendar_failure_does_not_propose_unverified_slots(ctx):
    p = acme_awaiting(ctx)
    ctx.calendar.fail_freebusy = ["transient", "transient", "transient"]
    p = reply(ctx, p, "Interested, happy to chat.")
    assert p["times_proposed"] == [] and "CALENDAR_UNAVAILABLE" in open_reasons(ctx, p["id"])
    assert len(agent_mails(ctx)) == 1
