"""Project assistant: answers only about the project, reads real data, and acts (with confirmation)."""
from helpers import agent_mails, run_campaign
from sprinto_agent import assistant, outbox, store


def _ask(ctx, msg, conv="t"):
    return assistant.ask(ctx, conv, msg)


def test_refuses_off_topic(ctx):
    r = _ask(ctx, "What's the weather in Paris?")
    assert r["answer"] == assistant.REFUSAL and r["tools"] == [] and r["actions"] == []


def test_answers_last_run_and_attention_from_data(ctx):
    ctx.settings.require_approval = True
    run_campaign(ctx, ["acmecloud.example"])
    r = _ask(ctx, "When did we do the last run?")
    assert r["tools"] == ["runs"] and "last ran" in r["answer"]
    r = _ask(ctx, "What needs immediate attention?")
    assert "AcmeCloud" in r["answer"] and "Approve" in r["answer"]


def test_deal_status(ctx):
    run_campaign(ctx, ["acmecloud.example"])
    r = _ask(ctx, "What happened with AcmeCloud?")
    assert r["tools"] == ["company_details"] and r["answer"].startswith("AcmeCloud")
    assert any(l["href"].startswith("#/companies/") for l in r["links"])


def test_send_needs_confirmation(ctx):
    ctx.settings.require_approval = True
    run_campaign(ctx, ["acmecloud.example"])
    r = _ask(ctx, "Approve and send the AcmeCloud email")
    [act] = r["actions"]
    assert agent_mails(ctx) == []                     # proposing sends nothing
    res = assistant.confirm(ctx, act["id"])
    assert res["status"] == "done" and len(agent_mails(ctx)) == 1
    assert assistant.confirm.__name__                  # second confirm is refused
    try:
        assistant.confirm(ctx, act["id"])
        raise AssertionError("should refuse")
    except ValueError:
        pass


def test_follow_up_is_drafted_then_sent_on_confirm(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    assert p["stage"] == "AWAITING_REPLY"            # auto-send in this test setup
    r = _ask(ctx, "Do a follow up with Priya at AcmeCloud")
    assert r["tools"] == ["draft_follow_up"]
    [pending] = outbox.listing(ctx.conn, "pending")
    assert pending["purpose"] == "follow_up" and len(agent_mails(ctx)) == 1
    [act] = r["actions"]
    assistant.confirm(ctx, act["id"])
    assert len(agent_mails(ctx)) == 2


def test_cancel_changes_nothing(ctx):
    r = _ask(ctx, "Start research on quietsaas.example")
    [act] = r["actions"]
    assistant.confirm(ctx, act["id"], cancel=True)
    assert ctx.conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0


def test_search_then_research_results(ctx):
    r = _ask(ctx, "Find saas companies with 50-200 employees in the US")
    assert r["tools"] == ["search_companies"] and "Found" in r["answer"]
    r = _ask(ctx, "research them")
    [act] = r["actions"]
    assistant.confirm(ctx, act["id"])
    assert ctx.conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] >= 1


def test_history_is_kept(ctx):
    _ask(ctx, "what needs my attention", conv="h")
    h = assistant.history(ctx.conn, "h")
    assert [m["role"] for m in h] == ["user", "assistant"]
