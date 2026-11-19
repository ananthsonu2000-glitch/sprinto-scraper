"""Human approval gate: nothing leaves the outbox without a person's approval."""
import pytest

from helpers import agent_mails, run_campaign
from sprinto_agent import outbox, store
from sprinto_agent.conversation import overview
from sprinto_agent.worker import Worker


@pytest.fixture
def actx(ctx):
    ctx.settings.require_approval = True
    return ctx


def _pending(ctx):
    return outbox.listing(ctx.conn, "pending")


def test_first_email_waits_for_approval_then_sends(actx):
    p = run_campaign(actx, ["acmecloud.example"])["acmecloud.example"]
    assert p["stage"] == "AWAITING_APPROVAL"
    assert agent_mails(actx) == []                       # nothing sent yet
    [row] = _pending(actx)
    assert row["purpose"] == "initial_outreach" and row["company"]
    Worker(actx, owner="w").tick()                       # ticking again does not send
    assert agent_mails(actx) == []

    sent = outbox.approve(actx, row["id"], body=row["body"] + "\n\nP.S. edited by a person")
    assert sent["status"] == "sent"
    p = store.get_prospect(actx.conn, p["id"])
    assert p["stage"] == "AWAITING_REPLY" and p["gmail_thread_id"]
    [mail] = agent_mails(actx)
    assert "edited by a person" in mail["body"]
    assert p["draft"]["edited_by_human"] is True
    with pytest.raises(outbox.OutboxError):
        outbox.approve(actx, row["id"])                 # cannot be sent twice


def test_reply_waits_for_approval(actx):
    p = run_campaign(actx, ["acmecloud.example"])["acmecloud.example"]
    outbox.approve(actx, _pending(actx)[0]["id"])
    p = store.get_prospect(actx.conn, p["id"])
    actx.mailbox.inject_prospect_reply(p["gmail_thread_id"], "Sounds interesting - happy to talk next week.")
    Worker(actx, owner="w").tick()
    assert len(agent_mails(actx)) == 1                   # the reply is drafted, not sent
    [row] = _pending(actx)
    assert row["purpose"] == "propose_times"
    assert "interesting" in row["context"]["their_message"]
    ov = overview(actx, p["id"])
    assert ov["next_actions"][0]["kind"] == "approve"
    outbox.approve(actx, row["id"])
    assert len(agent_mails(actx)) == 2


def test_discard_with_redraft_writes_a_new_email(actx):
    p = run_campaign(actx, ["acmecloud.example"])["acmecloud.example"]
    first = _pending(actx)[0]
    outbox.discard(actx, first["id"], note="too long", redraft=True)
    assert store.get_prospect(actx.conn, p["id"])["stage"] == "PAIN_READY"
    Worker(actx, owner="w").tick()
    [second] = _pending(actx)
    assert second["id"] != first["id"] and second["op_id"] != first["op_id"]
    outbox.discard(actx, second["id"])
    p = store.get_prospect(actx.conn, p["id"])
    assert p["stage"] == "DONE" and p["terminal_status"] == "REJECTED_BY_REVIEWER"
    assert agent_mails(actx) == []
