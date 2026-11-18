"""Contact selection, pain hypotheses, email drafting and QA."""
from helpers import agent_mails, run_campaign

from sprinto_agent import store
from sprinto_agent.email_gen import mandatory_checks
from sprinto_agent.pain import verify_hypothesis
from sprinto_agent.schemas import Claim, EmailDraft, PainLevels, PainProposal


def test_contact_selection_evaluates_all_candidates(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    # first Hunter result is a marketing VP; the GRC leader is preferred
    assert p["contact"]["name"] == "Priya Raman"
    assert p["contact"]["role"] == "compliance_grc"
    cands = {c["name"]: c for c in p["contact_candidates"]}
    assert cands["Mark Chen"]["passed"] is False
    assert cands["Daniel Ortiz"]["passed"] is True  # CISO passes but ranks below GRC
    assert cands[""]["score_breakdown"]["identity_quality"] == 0  # generic address, no name


def test_missing_contact_results_in_skip(ctx):
    p = run_campaign(ctx, ["nocontact.example"])["nocontact.example"]
    assert p["qualification_status"] == "QUALIFIED"
    assert p["terminal_status"] == "NO_SUITABLE_CONTACT"
    assert agent_mails(ctx) == []


def test_hunter_quota_pauses_lookups_with_reason(ctx):
    ctx.hunter.force_error = "quota"
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    assert p["stage"] == "WAITING_INTEGRATION"
    st = store.get_integration_status(ctx.conn, "hunter")
    assert st["status"] == "paused_quota"
    assert agent_mails(ctx) == []


def test_pain_hypotheses_stored_with_required_fields(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    hs = p["pain_hypotheses"]
    assert len(hs) == 2 and hs[0]["relevant_capability"] != hs[1]["relevant_capability"]
    for h in hs:
        for k in ("observed_fact", "evidence_ids", "hypothesis", "role_relevance", "possible_business_consequence",
                  "relevant_capability", "score_breakdown"):
            assert h[k]
        assert h["total"] >= 7 and h["score_breakdown"]["evidence"] >= 2 and h["score_breakdown"]["honest"] == 1


def _hyp(**kw):
    base = dict(observed_fact="x", evidence_ids=["e1", "e2"], hypothesis="Audit prep may take coordination.",
                role_relevance="r", possible_business_consequence="could add friction",
                relevant_capability="evidence_collection",
                levels=PainLevels(evidence=3, recipient=2, consequence=2, capability=2, honest=1))
    base.update(kw)
    return PainProposal(**base)


def test_unsupported_pain_claims_fail(ctx):
    ev = {"e1": {"excerpt": "We hold a SOC 2 Type II report."}, "e2": {"excerpt": "Customers include Globex Bank."}}
    caps = ctx.versions.capability_ids()
    rub = ctx.versions.rubric
    assert verify_hypothesis(_hyp(), ev, caps, rub)["passed"]
    assert not verify_hypothesis(_hyp(evidence_ids=[]), ev, caps, rub)["passed"]
    assert not verify_hypothesis(_hyp(evidence_ids=["fake"]), ev, caps, rub)["passed"]
    invented = verify_hypothesis(_hyp(hypothesis="Your team may spend 20 hours a week on manual spreadsheets."), ev, caps, rub)
    assert not invented["passed"] and invented["score_breakdown"]["honest"] == 0
    deficiency = verify_hypothesis(_hyp(hypothesis="You might be non-compliant with ISO 27001."), ev, caps, rub)
    assert not deficiency["passed"]
    assert not verify_hypothesis(_hyp(hypothesis="You are definitely struggling with audits."), ev, caps, rub)["passed"]
    assert not verify_hypothesis(_hyp(relevant_capability="ai_magic"), ev, caps, rub)["passed"]


def test_email_passes_qa_and_unsupported_claims_fail(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    assert p["email_qa"]["passed"] and p["email_qa"]["score"] >= 8
    ev = store.evidence_map(ctx.conn, p["id"])
    good = p["draft"]
    caps, rub = ctx.versions.capability_ids(), ctx.versions.rubric

    def check(body=None, subject=None, claims=None):
        d = EmailDraft(subject=subject or good["subject"], body=body or good["body"],
                       claims=[Claim(**c) for c in (claims if claims is not None else good["claims"])],
                       capability_id=good["capability_id"])
        return {k: v["passed"] for k, v in mandatory_checks(d, p, ev, caps, rub).items()}

    assert all(check().values())
    assert not check(body=good["body"].replace("Would a short", "Sprinto could save you 40 hours a month. Would a short"))["no_invented_metrics_guarantees_urgency"]
    assert not check(body=good["body"].replace("Would a short", "You are not SOC 2 compliant yet. Would a short"))["no_unsupported_deficiency_claims"]
    assert not check(subject="Re: our call")["no_false_re_or_follow_up"]
    assert not check(body=good["body"].replace("I was reading", "Following up on my note, I was reading"))["no_false_re_or_follow_up"]
    assert not check(claims=[{"text": "AcmeCloud raised a Series C", "evidence_ids": ["nope"]}])["evidence_for_every_claim"]
    assert not check(body=good["body"].replace("Hi Priya", "Hi Daniel"))["correct_person_company"]
    assert not check(body=good["body"].replace("Sprinto may", "Sprinto guarantees you pass. It may"))["no_invented_metrics_guarantees_urgency"]


def test_email_qa_failure_after_two_revisions(ctx, monkeypatch):
    from sprinto_agent.llm.simulated import SimulatedLLM
    orig = SimulatedLLM._draft

    def bad_draft(self, c):
        d = orig(self, c)
        d.question = "This is urgent. " + d.question
        return d
    monkeypatch.setattr(SimulatedLLM, "_draft", bad_draft)
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    assert p["terminal_status"] == "EMAIL_QA_FAILED"
    assert len(p["email_qa"]["attempts"]) == 3  # initial + 2 revisions
    assert ctx.llm.calls.count("evaluate_email") == 3  # separate evaluation step each time
    assert agent_mails(ctx) == []
