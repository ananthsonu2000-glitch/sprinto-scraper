from sprinto_agent import store
from sprinto_agent.outreach import create_campaign
from sprinto_agent.worker import Worker


def run_campaign(ctx, domains, ticks=1):
    r = create_campaign(ctx, "test", domains)
    w = Worker(ctx, owner="test-worker")
    for _ in range(ticks):
        w.tick()
    return {store.get_prospect(ctx.conn, pid)["normalized_domain"]: store.get_prospect(ctx.conn, pid)
            for pid in r["prospect_ids"]}


def acme_awaiting(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    assert p["stage"] == "AWAITING_REPLY", (p["stage"], p["escalation_reason"])
    return p


def reply(ctx, p, text):
    ctx.mailbox.inject_prospect_reply(p["gmail_thread_id"], text)
    Worker(ctx, owner="test-worker").tick()
    return store.get_prospect(ctx.conn, p["id"])


def agent_mails(ctx):
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM sim_mailbox WHERE author_kind='agent' ORDER BY id")]


def open_reasons(ctx, pid):
    return [r["reason"] for r in ctx.conn.execute(
        "SELECT reason FROM escalations WHERE prospect_id=? AND status='open'", (pid,))]
