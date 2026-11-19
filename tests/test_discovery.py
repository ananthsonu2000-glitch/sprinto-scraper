"""Company discovery: filters -> candidates with honest per-filter matching."""
import json

import httpx
import pytest
from pydantic import ValidationError

from sprinto_agent import store
from sprinto_agent.discovery import DiscoveryFilters, add_to_run, hunter_body, run_search, set_candidate_domain
from sprinto_agent.integrations.hunter import HunterClient
from sprinto_agent.schemas import CompanyEvidenceRef, DiscoveredCompanies, DiscoveredCompany
from sprinto_agent.worker import Worker

ICP = dict(industries=["Software Development", "Data Infrastructure and Analytics"],
           headcount=["11-50", "51-200", "201-500"], funding_series=["series_a", "series_b"],
           funded_within_months=6, nrr_min_percent=110)


def by_name(res):
    return {c["name"]: c for c in res["candidates"]}


def test_filters_are_validated():
    with pytest.raises(ValidationError):
        DiscoveryFilters(industries=["Made Up Industry"])
    with pytest.raises(ValidationError):
        DiscoveryFilters(headcount=["12-34"])
    with pytest.raises(ValidationError):
        DiscoveryFilters(countries=["USA"])
    assert DiscoveryFilters(countries=["us", " gb "]).countries == ["US", "GB"]


def test_search_combines_sources_and_matches_honestly(ctx):
    res = run_search(ctx, DiscoveryFilters(**ICP))
    c = by_name(res)
    # NoContact: Hunter confirms industry + size; news states Series A in window and 125% NRR
    assert c["NoContact Analytics"]["match_level"] == "strong"
    assert c["NoContact Analytics"]["matches"]["nrr"]["status"] == "matched"
    # AcmeCloud: everything confirmed except NRR, which is unknown (not assumed, not excluded)
    a = c["AcmeCloud"]
    assert a["match_level"] == "partial"
    assert a["matches"]["nrr"]["status"] == "unknown"
    assert a["matches"]["funding"]["status"] == "matched" and "Series B" in a["matches"]["funding"]["basis"]
    # QuietSaaS raised a seed round -> contradicts the Series A/B filter
    assert c["QuietSaaS"]["match_level"] == "excluded"
    assert c["QuietSaaS"]["matches"]["funding"]["status"] == "not_matched"


def test_ignored_hunter_filters_are_not_claimed(ctx):
    res = run_search(ctx, DiscoveryFilters(funding_series=["series_b"], headcount=["201-500"], sources=["hunter_discover"]))
    rep = res["source_reports"][0]
    assert "funding" in rep["ignored_filters"] and "headcount" in rep["applied_filters"]
    acme = by_name(res)["AcmeCloud"]
    assert acme["matches"]["funding"]["status"] == "unknown"  # Hunter ignored it on this plan
    assert acme["matches"]["headcount"]["status"] == "matched"


def test_unverified_domain_needs_user_input_and_is_not_guessed(ctx):
    res = run_search(ctx, DiscoveryFilters(**ICP))
    led = by_name(res)["Ledgerly"]
    assert led["domain"] is None and led["match_level"] == "needs_domain"
    out = add_to_run(ctx, res["id"], [led["id"]])
    assert out["campaign_id"] is None and out["skipped"][0]["reason"] == "no domain yet"
    res2 = set_candidate_domain(ctx, led["id"], "https://www.Ledgerly.example/about")
    led2 = by_name(res2)["Ledgerly"]
    assert led2["domain"] == "ledgerly.example" and led2["match_level"] == "partial"


def test_domain_resolved_by_exact_name_via_hunter(ctx):
    res = run_search(ctx, DiscoveryFilters(funding_series=["seed"], sources=["news_search"]))
    q = by_name(res)["QuietSaaS"]  # article states no domain; Hunter exact-name lookup supplies it
    assert q["domain"] == "quietsaas.example"
    assert any("exact company-name lookup" in e["excerpt"] for e in q["evidence"])


def test_injected_sources_are_ignored(ctx):
    res = run_search(ctx, DiscoveryFilters(**ICP))
    rep = next(r for r in res["source_reports"] if r["source"] == "news_search")
    assert "https://news.example/sponsored" in rep["suspicious_sources_ignored"]
    assert all("evil" not in (c["domain"] or "") for c in res["candidates"])


class InventingLLM:
    simulated = True
    label = "inventing"

    def __init__(self, inner):
        self.inner = inner

    def web_research(self, *a, **k):
        return self.inner.web_research(*a, **k)

    def structured(self, task, system, context, schema):
        if task == "discover_extract":
            return DiscoveredCompanies(companies=[
                DiscoveredCompany(name="Phantom Corp", domain="phantom.example",
                                  evidence=[CompanyEvidenceRef(citation_index=0, signal="funding")],
                                  funding_series="Series B", nrr_text="150% net revenue retention"),
                DiscoveredCompany(name="AcmeCloud", domain="acme-guessed.example",
                                  evidence=[CompanyEvidenceRef(citation_index=0, signal="funding")]),
            ])
        return self.inner.structured(task, system, context, schema)


def test_invented_companies_and_domains_are_rejected(ctx):
    ctx.llm = InventingLLM(ctx.llm)
    res = run_search(ctx, DiscoveryFilters(funding_series=["series_b"], sources=["news_search"]))
    names = by_name(res)
    assert "Phantom Corp" not in names  # not named in any retrieved citation
    rep = res["source_reports"][0]
    assert "Phantom Corp" in rep["dropped_unsupported"]
    assert names["AcmeCloud"]["domain"] == "acmecloud.example"  # guessed domain rejected; Hunter name lookup used


def test_add_selected_creates_run_and_pipeline_runs(ctx):
    res = run_search(ctx, DiscoveryFilters(**ICP))
    c = by_name(res)
    out = add_to_run(ctx, res["id"], [c["AcmeCloud"]["id"], c["NoContact Analytics"]["id"], c["QuietSaaS"]["id"]], "ICP run")
    assert sorted(out["added"]) == ["acmecloud.example", "nocontact.example"]
    assert any(s["name"] == "QuietSaaS" for s in out["skipped"])  # excluded candidates are not added
    again = add_to_run(ctx, res["id"], [c["AcmeCloud"]["id"]])
    assert again["campaign_id"] is None  # no duplicate add
    Worker(ctx).tick()
    stages = {r["normalized_domain"]: r["stage"] for r in ctx.conn.execute(
        "SELECT normalized_domain, stage FROM prospects WHERE campaign_id=?", (out["campaign_id"],))}
    assert stages["acmecloud.example"] == "AWAITING_REPLY"


def test_source_failure_is_reported_not_faked(ctx):
    ctx.hunter.force_error = "auth"
    res = run_search(ctx, DiscoveryFilters(**ICP))
    rep = {r["source"]: r for r in res["source_reports"]}
    assert rep["hunter_discover"]["ok"] is False and "auth" in rep["hunter_discover"]["error"]
    assert res["status"] == "partial"
    assert all("hunter_discover" not in c["sources"] for c in res["candidates"])


def test_non_commercial_organisations_excluded_by_default(ctx, monkeypatch):
    def fake_discover(body):
        assert body["company_type"]["exclude"][0] == "educational"
        return {"data": [{"domain": "utu.ac.in", "organization": "Uka Tarsadia University"},
                         {"domain": "mkcl.org", "organization": "Maharashtra Knowledge Corporation"},
                         {"domain": "hrone.cloud", "organization": "HROne"}],
                "meta": {"filters": {"industry": {}}}}
    monkeypatch.setattr(ctx.hunter, "discover", fake_discover)
    res = run_search(ctx, DiscoveryFilters(industries=["Software Development"], sources=["hunter_discover"]))
    names = [c["name"] for c in res["candidates"]]
    assert "Uka Tarsadia University" not in names and "HROne" in names
    assert "Uka Tarsadia University" in res["source_reports"][0]["skipped_non_commercial"]


def test_missing_keys_report_setup_required(ctx):
    from sprinto_agent.context import UnconfiguredHunter, UnconfiguredLLM
    ctx.hunter = UnconfiguredHunter()
    ctx.llm = UnconfiguredLLM("ANTHROPIC_API_KEY is not set")
    res = run_search(ctx, DiscoveryFilters(**ICP, sources=["hunter_discover", "news_search"]))
    assert res["status"] == "failed" and res["candidates"] == []
    assert all("setup_required" in r["error"] for r in res["source_reports"])


def test_real_hunter_discover_request_shape(settings, ctx):
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"domain": "x.example", "organization": "X", "emails_count": {}}],
                                         "meta": {"filters": {"industry": {"include": ["Software Development"]}}}})
    h = HunterClient("k", transport=httpx.MockTransport(handler))
    f = DiscoveryFilters(industries=["Software Development"], headcount=["51-200"], countries=["US"],
                         funding_series=["series_a"], funded_within_months=6, hiring_roles=["GRC"], keywords=["payroll"])
    resp = h.discover(hunter_body(f, store.clock.now()))
    b = seen["body"]
    assert seen["path"] == "/v2/discover"
    assert b["industry"] == {"include": ["Software Development"]} and b["headcount"] == ["51-200"]
    assert b["headquarters_location"] == {"include": [{"country": "US"}]}
    assert b["funding"]["series"] == ["series_a"] and b["funding"]["date"]["from"] == "2026-05-19"
    assert b["job_openings"] == {"job_title": {"include": ["GRC"]}}
    assert resp["data"][0]["domain"] == "x.example"


def test_discovery_api_endpoints(settings, monkeypatch):
    from fastapi.testclient import TestClient

    import sprinto_agent.api as api
    monkeypatch.setattr(api, "_state", {})
    monkeypatch.setattr(api.Settings, "from_env", classmethod(lambda cls, **k: settings))
    client = TestClient(api.app)
    assert len(client.get("/api/discovery/options").json()["industries"]) > 400
    assert client.post("/api/discovery/search", json={}).status_code == 422
    r = client.post("/api/discovery/search", json=ICP).json()
    acme = next(c for c in r["candidates"] if c["name"] == "AcmeCloud")
    add = client.post(f"/api/discovery/searches/{r['id']}/add", json={"candidate_ids": [acme["id"]]}).json()
    assert add["added"] == ["acmecloud.example"]
    api._state["ctx"].conn.close()
