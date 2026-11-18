"""Idempotency, crash recovery, reconciliation, concurrency, escalation handling."""
import pytest
from conftest import new_ctx
from helpers import acme_awaiting, agent_mails, open_reasons, reply

from sprinto_agent import store
from sprinto_agent.human import HumanActionError, resolve_escalation
from sprinto_agent.outreach import create_campaign, process_prospect
from sprinto_agent.worker import Worker


def test_repeated_jobs_do_not_repeat_actions(ctx):
    p = acme_awaiting(ctx)
    w = Worker(ctx)
    w.tick()
    w.tick()
    process_prospect(ctx, p["id"])
    assert len(agent_mails(ctx)) == 1


def test_crash_after_send_before_state_save_does_not_resend(ctx, monkeypatch):
    r = create_campaign(ctx, "t", ["acmecloud.example"])
    pid = r["prospect_ids"][0]
    import sprinto_agent.outreach as outreach
    real = outreach.send_initial

    def crash_after_send(c, p):
        from sprinto_agent.tools import run_tool
        d = p["draft"]
        run_tool(c, "send_email", {"prospect_id": p["id"], "op_id": f"outreach:{p['campaign_id']}:{p['normalized_domain']}",
                                   "purpose": "initial_outreach", "subject": d["subject"], "body": d["body"]})
        raise SystemExit("simulated process crash")
    monkeypatch.setitem(outreach.STAGE_HANDLERS, "DRAFT_READY", crash_after_send)
    with pytest.raises(SystemExit):
        process_prospect(ctx, pid)
    assert store.get_prospect(ctx.conn, pid)["stage"] == "DRAFT_READY"
    monkeypatch.setitem(outreach.STAGE_HANDLERS, "DRAFT_READY", real)
    ctx2 = new_ctx(ctx.settings)  # "restarted process"
    p = process_prospect(ctx2, pid)
    assert p["stage"] == "AWAITING_REPLY" and p["gmail_thread_id"]
    assert len(agent_mails(ctx2)) == 1
    ctx2.conn.close()


def test_restart_resumes_from_persisted_research_state(ctx, monkeypatch):
    r = create_campaign(ctx, "t", ["acmecloud.example"])
    pid = r["prospect_ids"][0]
    import sprinto_agent.outreach as outreach
    monkeypatch.setitem(outreach.STAGE_HANDLERS, "QUALIFIED", lambda c, p: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        process_prospect(ctx, pid)
    assert store.get_prospect(ctx.conn, pid)["stage"] == "QUALIFIED"
    fetched = len(ctx.fetcher.calls)
    monkeypatch.undo()
    ctx2 = new_ctx(ctx.settings)
    p = process_prospect(ctx2, pid)
    assert p["stage"] == "AWAITING_REPLY"
    assert ctx2.fetcher.calls == []  # research not repeated after restart
    assert fetched > 0
    ctx2.conn.close()


def test_duplicate_replies_are_processed_once(ctx):
    p = acme_awaiting(ctx)
    ctx.mailbox.inject_prospect_reply(p["gmail_thread_id"], "Interested, happy to chat.")
    Worker(ctx).tick()
    Worker(ctx).tick()
    from sprinto_agent.tools import run_tool
    run_tool(ctx, "read_thread_replies", {"prospect_id": p["id"]})  # re-reading the same thread
    Worker(ctx).tick()
    assert ctx.conn.execute("SELECT COUNT(*) FROM messages WHERE direction='inbound'").fetchone()[0] == 1
    assert len(agent_mails(ctx)) == 2


def test_agent_emails_do_not_trigger_reply_loop(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    for _ in range(3):
        Worker(ctx).tick()
    inbound = ctx.conn.execute("SELECT author_kind FROM messages WHERE direction='inbound'").fetchall()
    assert [r[0] for r in inbound] == ["simulated_prospect"]
    assert len(agent_mails(ctx)) == 2  # outreach + one slot proposal, nothing self-triggered


def test_reply_graph_resumes_from_checkpoint_after_crash(ctx, monkeypatch):
    p = acme_awaiting(ctx)
    ctx.mailbox.inject_prospect_reply(p["gmail_thread_id"], "Interested, happy to chat.")
    import sprinto_agent.reply_agent as ra
    real = ra._send_reply
    calls = {"n": 0}

    def send_then_crash(c, pr, st, action, purpose, body):
        out = real(c, pr, st, action, purpose, body)
        calls["n"] += 1
        raise SystemExit("crash after send")
    monkeypatch.setattr(ra, "_send_reply", send_then_crash)
    with pytest.raises(SystemExit):
        Worker(ctx).tick()
    row = ctx.conn.execute("SELECT processing_status FROM messages WHERE direction='inbound'").fetchone()
    assert row[0] == "processing"
    monkeypatch.setattr(ra, "_send_reply", real)
    store.release_lease(ctx.conn, p["id"], store.get_prospect(ctx.conn, p["id"])["lease_owner"])
    ctx2 = new_ctx(ctx.settings)
    Worker(ctx2).tick()
    p2 = store.get_prospect(ctx2.conn, p["id"])
    assert p2["reply_status"] == "SLOTS_PROPOSED"
    assert len(agent_mails(ctx2)) == 2  # proposal not re-sent
    assert ctx2.conn.execute("SELECT processing_status FROM messages WHERE direction='inbound'").fetchone()[0] == "done"
    ctx2.extras["checkpoint_conn"].close()
    ctx2.conn.close()


def test_uncertain_send_reconciled_before_retry(ctx):
    r = create_campaign(ctx, "t", ["acmecloud.example"])
    ctx.mailbox.fail_next = ["timeout_after_store"]  # delivered, but we saw a timeout
    p = process_prospect(ctx, r["prospect_ids"][0])
    assert p["stage"] == "AWAITING_REPLY"
    assert len(agent_mails(ctx)) == 1
    op = ctx.conn.execute("SELECT * FROM operations WHERE kind='send_email'").fetchone()
    assert op["status"] == "succeeded" and op["attempts"] == 1


def test_uncertain_send_not_delivered_retries_once(ctx):
    r = create_campaign(ctx, "t", ["acmecloud.example"])
    ctx.mailbox.fail_next = ["timeout_before_store"]
    p = process_prospect(ctx, r["prospect_ids"][0])
    assert p["stage"] == "AWAITING_REPLY" and len(agent_mails(ctx)) == 1


def test_unresolvable_send_marks_send_uncertain_and_pauses(ctx):
    r = create_campaign(ctx, "t", ["acmecloud.example"])
    ctx.mailbox.fail_next = ["timeout_after_store"]
    ctx.mailbox.lookup_fails = True
    p = process_prospect(ctx, r["prospect_ids"][0])
    assert p["stage"] == "ESCALATED" and "SEND_UNCERTAIN" in open_reasons(ctx, p["id"])
    # a re-run must not blindly resend
    Worker(ctx).tick()
    assert len(agent_mails(ctx)) == 1
    esc = store.open_escalations(ctx.conn)[0]
    with pytest.raises(HumanActionError):
        resolve_escalation(ctx, esc["id"], "resume")  # not an allowed action for this reason
    ctx.mailbox.lookup_fails = False
    resolve_escalation(ctx, esc["id"], "confirm_not_sent", "checked Gmail Sent folder")
    # confirm_not_sent re-enables exactly one guarded attempt; reconciliation finds the real message first
    p = process_prospect(ctx, p["id"])
    assert len(agent_mails(ctx)) == 1 and p["stage"] == "AWAITING_REPLY"


def test_uncertain_booking_reconciled_no_duplicate_event(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    ctx.calendar.fail_create = ["timeout_after_create"]
    p = reply(ctx, p, "Option A works")
    assert p["meeting_status"] == "BOOKED"
    assert ctx.conn.execute("SELECT COUNT(*) FROM sim_calendar_events").fetchone()[0] == 1
    assert sum("Confirmed" in m["body"] for m in agent_mails(ctx)) == 1


def test_unresolvable_booking_marks_booking_uncertain(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    ctx.calendar.fail_create = ["timeout_after_create"]
    ctx.calendar.lookup_fails = True
    p = reply(ctx, p, "Option A works")
    assert p["stage"] == "ESCALATED" and "BOOKING_UNCERTAIN" in open_reasons(ctx, p["id"])
    assert all("Confirmed" not in m["body"] for m in agent_mails(ctx))


def test_leases_prevent_two_workers_on_same_conversation(ctx):
    p = acme_awaiting(ctx)
    assert store.acquire_lease(ctx.conn, p["id"], "worker-A", 300)
    assert not store.acquire_lease(ctx.conn, p["id"], "worker-B", 300)
    ctx.mailbox.inject_prospect_reply(p["gmail_thread_id"], "Interested, happy to chat.")
    Worker(ctx, owner="worker-B").tick()
    assert len(agent_mails(ctx)) == 1  # B could not touch the leased conversation
    store.release_lease(ctx.conn, p["id"], "worker-A")
    Worker(ctx, owner="worker-B").tick()
    assert len(agent_mails(ctx)) == 2


def test_stale_state_is_rejected(ctx):
    p = acme_awaiting(ctx)
    a = store.get_prospect(ctx.conn, p["id"])
    b = store.get_prospect(ctx.conn, p["id"])
    store.save_prospect(ctx.conn, a, reply_status="X")
    with pytest.raises(store.StaleStateError):
        store.save_prospect(ctx.conn, b, reply_status="Y")


def test_escalation_pauses_only_that_prospect_and_resume_is_controlled(ctx):
    r = create_campaign(ctx, "t", ["acmecloud.example", "nocontact.example"])
    ctx.mailbox.fail_next = ["auth"]
    Worker(ctx).tick()
    ps = {store.get_prospect(ctx.conn, x)["normalized_domain"]: store.get_prospect(ctx.conn, x) for x in r["prospect_ids"]}
    assert ps["acmecloud.example"]["stage"] == "ESCALATED"
    assert ps["nocontact.example"]["terminal_status"] == "NO_SUITABLE_CONTACT"  # batch continued
    esc = store.open_escalations(ctx.conn)[0]
    assert esc["reason"] == "SETUP_REQUIRED" and esc["allowed_actions"] == ["resume", "close"]
    resolve_escalation(ctx, esc["id"], "resume", "fixed credentials")
    Worker(ctx).tick()
    assert store.get_prospect(ctx.conn, ps["acmecloud.example"]["id"])["stage"] == "AWAITING_REPLY"


def test_activity_log_failure_preserves_action(ctx, monkeypatch):
    import sprinto_agent.store as st
    p = acme_awaiting(ctx)
    real_exec = ctx.conn.execute

    class FlakyConn:
        def __getattr__(self, k):
            return getattr(ctx.conn, k)

        def execute(self, sql, *a):
            if sql.startswith("INSERT INTO activity_log"):
                import sqlite3
                raise sqlite3.OperationalError("disk I/O error")
            return real_exec(sql, *a)
    monkeypatch.setattr(st, "FALLBACK_LOG", ctx.settings.db_path + ".fallback.jsonl")
    from pathlib import Path
    monkeypatch.setattr(st, "FALLBACK_LOG", Path(ctx.settings.db_path + ".fallback.jsonl"))
    ok = st.log_activity(FlakyConn(), p["id"], "crm_update", "logged", "test")
    assert ok is False
    assert store.get_prospect(ctx.conn, p["id"])["stage"] == "AWAITING_REPLY"
    assert st.replay_fallback_log(ctx.conn) == 1
