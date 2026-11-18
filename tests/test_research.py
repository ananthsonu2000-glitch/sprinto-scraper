"""Website research, extraction, evidence and qualification."""
from helpers import run_campaign

from sprinto_agent import store
from sprinto_agent.research.extraction import apply_extraction, empty_profile
from sprinto_agent.research.qualification import score
from sprinto_agent.schemas import (Citation, DimensionProposal, ExtractionOutput, FieldExtraction,
                                   QualificationProposal)


def test_existing_compliance_does_not_disqualify(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    assert p["qualification_status"] == "QUALIFIED"
    claims = p["company_profile"]["compliance_claims"]["value"]
    # precise wording preserved, distinct claims kept distinct
    assert "SOC 2 Type II report" in claims and "ISO 27001 certified" in claims
    assert p["qualification_detail"]["disqualifier"] == "none"


def test_missing_soc2_evidence_is_unknown_not_negative(ctx):
    p = run_campaign(ctx, ["quietsaas.example"])["quietsaas.example"]
    cw = p["qualification_detail"]["dimensions"]["compliance_workload"]
    assert cw["level"] == 0 and cw["zero_reason"] == "unknown"
    assert p["company_profile"]["compliance_claims"]["status"] == "unknown"
    assert p["company_profile"]["compliance_claims"]["value"] is None
    blob = str(p["company_profile"]).lower()
    assert "not compliant" not in blob and "no soc" not in blob


def test_blocked_pages_are_retrieval_failures_not_negative(ctx):
    p = run_campaign(ctx, ["quietsaas.example"])["quietsaas.example"]
    pages = {pg["url"]: pg for pg in store.list_pages(ctx.conn, p["id"])}
    assert pages["https://quietsaas.example/security"]["status"] == "blocked"
    assert p["terminal_status"] == "INSUFFICIENT_EVIDENCE"  # not LOW_PRIORITY / not negative


def test_unknown_funding_stage_remains_unknown(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    assert p["company_profile"]["funding_stage"]["status"] == "unknown"
    assert p["company_profile"]["funding_stage"]["value"] is None


def test_llm_cannot_infer_funding_or_fabricate_excerpts(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    pages = store.list_pages(ctx.conn, p["id"])
    home = pages[0]
    out = ExtractionOutput(fields=[
        # inferred from appearance: cites a real excerpt that does not state funding
        FieldExtraction(name="funding_stage", value="Series B", status="inferred",
                        citations=[Citation(url=home["url"], excerpt="AcmeCloud is a cloud-based payroll platform for growing companies.")]),
        # fabricated excerpt
        FieldExtraction(name="employee_count", value="5000", status="observed",
                        citations=[Citation(url=home["url"], excerpt="AcmeCloud employs 5000 people worldwide.")]),
    ])
    prof, notes = apply_extraction(ctx.conn, p, pages, out, empty_profile("acmecloud.example"))
    assert prof["funding_stage"]["value"] is None and prof["funding_stage"]["status"] == "unknown"
    assert prof["employee_count"]["value"] is None
    assert any("not found verbatim" in n for n in notes)


def test_conflicting_claims_preserved(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    pages = store.list_pages(ctx.conn, p["id"])
    prof = empty_profile("acmecloud.example")
    sec = next(pg for pg in pages if pg["url"].endswith("/security"))
    home = pages[0]
    a = ExtractionOutput(fields=[FieldExtraction(name="business_model", value="cloud-based", status="observed",
                                                 citations=[Citation(url=home["url"], excerpt="AcmeCloud is a cloud-based payroll platform for growing companies.")])])
    b = ExtractionOutput(fields=[FieldExtraction(name="business_model", value="on-premise", status="observed",
                                                 citations=[Citation(url=sec["url"], excerpt="Customer data is encrypted at rest and in transit.")])])
    prof, _ = apply_extraction(ctx.conn, p, pages, a, prof)
    prof, _ = apply_extraction(ctx.conn, p, pages, b, prof)
    assert prof["business_model"]["status"] == "conflicting"
    assert set(prof["business_model"]["value"]) == {"cloud-based", "on-premise"}


def test_evidence_records_have_required_fields(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    ev = store.evidence_map(ctx.conn, p["id"])
    assert ev
    pages = {pg["final_url"]: pg for pg in store.list_pages(ctx.conn, p["id"])}
    for e in ev.values():
        assert e["source_url"] and e["excerpt"] and e["retrieved_at"] and e["source_type"]
        assert e["excerpt"] in pages[e["source_url"]]["text"]  # validated against retrieved text
    hiring = [e for e in ev.values() if "GRC Analyst" in e["excerpt"]]
    assert hiring and hiring[0]["published_at"] == "2026-10-05"


def test_research_respects_cap_and_no_refetch(ctx):
    p = run_campaign(ctx, ["quietsaas.example"])["quietsaas.example"]
    pol = ctx.policy["research"]
    pages = store.list_pages(ctx.conn, p["id"])
    assert p["research_rounds"] <= pol["max_additional_rounds"]
    assert len(pages) <= 1 + pol["initial_extra_pages"] + pol["max_additional_rounds"] * pol["max_new_pages_per_round"]
    for r in range(1, p["research_rounds"] + 1):
        assert len([pg for pg in pages if pg["research_round"] == r]) <= pol["max_new_pages_per_round"]
    assert len(ctx.fetcher.calls) == len(set(ctx.fetcher.calls))  # never fetched the same URL twice


def test_disqualifies_unrelated_and_competitor(ctx):
    ps = run_campaign(ctx, ["brightbakery.example", "certifyfast.example"])
    assert ps["brightbakery.example"]["terminal_status"] == "DISQUALIFIED"
    assert ps["brightbakery.example"]["qualification_detail"]["disqualifier"] == "unrelated_business"
    assert ps["certifyfast.example"]["terminal_status"] == "DISQUALIFIED"
    assert ps["certifyfast.example"]["qualification_detail"]["disqualifier"] == "compliance_platform_competitor"


def _ev(ids):
    return {i: {"id": i, "excerpt": "x", "published_at": None} for i in ids}


def test_scoring_requires_valid_evidence_and_rules(ctx):
    rubric = ctx.versions.rubric
    full = QualificationProposal(dimensions=[
        DimensionProposal(dimension=d, level=2, evidence_ids=["e1"], reason="r")
        for d in ["business_fit", "customer_trust", "compliance_workload", "delivery_complexity"]
    ] + [DimensionProposal(dimension="reason_now", level=2, evidence_ids=["e1"], reason="undated")],
        disqualifier="none", disqualifier_evidence_ids=[], gaps=[])
    res = score(full, _ev(["e1"]), rubric)
    assert res["dimensions"]["reason_now"]["level"] == 0  # undated -> outside window
    assert res["total"] == 80 and res["status"] == "QUALIFIED"
    # fabricated evidence ID -> level ignored
    fake = full.model_copy(deep=True)
    for d in fake.dimensions:
        d.evidence_ids = ["made_up"]
    res2 = score(fake, _ev(["e1"]), rubric)
    assert res2["total"] == 0 and all(d["zero_reason"] == "unknown" for d in res2["dimensions"].values())
    # business fit 1 can never qualify even with a high total
    fit1 = full.model_copy(deep=True)
    fit1.dimensions[0].level = 1
    assert score(fit1, _ev(["e1"]), rubric)["status"] != "QUALIFIED"


def test_failed_account_does_not_stop_batch(ctx, monkeypatch):
    orig = ctx.fetcher.fetch

    def boom(url):
        if "brightbakery" in url:
            raise RuntimeError("unexpected parser crash")
        return orig(url)
    monkeypatch.setattr(ctx.fetcher, "fetch", boom)
    ps = run_campaign(ctx, ["brightbakery.example", "acmecloud.example"])
    assert ps["brightbakery.example"]["stage"] == "ESCALATED"
    assert "UNEXPECTED_ERROR" in ps["brightbakery.example"]["escalation_reason"]
    assert ps["acmecloud.example"]["stage"] == "AWAITING_REPLY"


def test_transient_fetch_failures_retry_three_times(ctx):
    p = run_campaign(ctx, ["flakyco.example"])["flakyco.example"]
    home_calls = [u for u in ctx.fetcher.calls if u == "https://flakyco.example/"]
    assert len(home_calls) == 3
    assert p["terminal_status"] == "INSUFFICIENT_EVIDENCE"


def test_domain_normalization_and_dedupe(ctx):
    from sprinto_agent.outreach import create_campaign
    r = create_campaign(ctx, "t", ["https://WWW.AcmeCloud.example/about?x=1", "acmecloud.example", "localhost",
                                   "10.0.0.1", "not a domain", "metadata.google.internal"])
    assert r["domains"] == ["acmecloud.example"]
    assert set(r["rejected"]) == {"localhost", "10.0.0.1", "not a domain", "metadata.google.internal"}
