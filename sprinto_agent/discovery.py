"""Company discovery: find candidate companies from filters instead of typing domains.

Sources
  hunter_discover - Hunter Discover (company search by industry, headcount, location,
                    hiring, keywords; funding on paid plans). Free call per Hunter docs.
  news_search     - Claude's web search tool over recent news, then a structured
                    extraction step. Every company must be named in a retrieved citation.

Honesty rules (enforced in code)
  * A filter counts as "matched" only with a basis: Hunter actually APPLIED that filter
    (Hunter silently ignores filters your plan lacks), or a cited source states it.
  * Otherwise the filter is "unknown" — never assumed. A stated contradiction is "not_matched".
  * NRR is almost never public for private companies: it only matches when a source states it.
  * Domains are used only if stated in a source or returned by Hunter; otherwise the
    candidate needs a domain from you before it can be added to a run.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator

from . import clock, store
from .config import CONFIG_DIR
from .errors import IntegrationError
from .research.url_guard import normalize_domain
from .retry import with_retries
from .schemas import DiscoveredCompanies

HEADCOUNT_RANGES = ["1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001-10000", "10001+"]
FUNDING_SERIES = ["pre_seed", "seed", "pre_series_a", "series_a", "pre_series_b", "series_b", "pre_series_c", "series_c+", "other"]
SERIES_LABEL = {"pre_seed": "Pre-seed", "seed": "Seed", "pre_series_a": "Pre-Series A", "series_a": "Series A",
                "pre_series_b": "Pre-Series B", "series_b": "Series B", "pre_series_c": "Pre-Series C",
                "series_c+": "Series C or later", "other": "Other"}
SOURCES = ["hunter_discover", "news_search"]


def industries() -> list[str]:
    return json.loads((CONFIG_DIR / "hunter_industries.json").read_text(encoding="utf-8"))


class DiscoveryFilters(BaseModel):
    industries: list[str] = Field(default_factory=list)
    headcount: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list, description="ISO 3166-1 alpha-2, e.g. US, GB, IN")
    funding_series: list[str] = Field(default_factory=list)
    funded_within_months: int | None = None
    hiring_roles: list[str] = Field(default_factory=list)
    nrr_min_percent: float | None = None
    keywords: list[str] = Field(default_factory=list)
    max_results: int = 25
    exclude_non_commercial: bool = True  # skip universities, government bodies and non-profits
    sources: list[str] = Field(default_factory=lambda: list(SOURCES))

    @field_validator("industries")
    @classmethod
    def _ind(cls, v):
        known = set(industries())
        bad = [x for x in v if x not in known]
        if bad:
            raise ValueError(f"unknown industries {bad}; pick from the list")
        return v

    @field_validator("headcount")
    @classmethod
    def _hc(cls, v):
        bad = [x for x in v if x not in HEADCOUNT_RANGES]
        if bad:
            raise ValueError(f"invalid headcount ranges {bad}")
        return v

    @field_validator("funding_series")
    @classmethod
    def _fs(cls, v):
        bad = [x for x in v if x not in FUNDING_SERIES]
        if bad:
            raise ValueError(f"invalid funding series {bad}")
        return v

    @field_validator("countries")
    @classmethod
    def _cc(cls, v):
        v = [x.strip().upper() for x in v if x.strip()]
        if any(not re.fullmatch(r"[A-Z]{2}", x) for x in v):
            raise ValueError("countries must be 2-letter ISO codes, e.g. US, GB, IN")
        return v

    @field_validator("max_results")
    @classmethod
    def _mr(cls, v):
        return max(1, min(100, v))

    @field_validator("sources")
    @classmethod
    def _src(cls, v):
        bad = [x for x in v if x not in SOURCES]
        if bad or not v:
            raise ValueError(f"sources must be a non-empty subset of {SOURCES}")
        return v

    def requested(self) -> list[str]:
        out = []
        for name, val in [("industry", self.industries), ("headcount", self.headcount), ("location", self.countries),
                          ("funding", self.funding_series or self.funded_within_months), ("hiring", self.hiring_roles),
                          ("nrr", self.nrr_min_percent), ("keywords", self.keywords)]:
            if val:
                out.append(name)
        return out


# ------------------------------------------------------------------ Hunter Discover

HUNTER_FILTER_NAMES = {"industry": "industry", "headcount": "headcount", "headquarters_location": "location",
                       "funding": "funding", "job_openings": "hiring", "keywords": "keywords"}  # company_type is internal


def hunter_body(f: DiscoveryFilters, now: datetime) -> dict:
    body: dict = {"limit": f.max_results}
    if f.industries:
        body["industry"] = {"include": f.industries}
    if f.headcount:
        body["headcount"] = f.headcount
    if f.countries:
        body["headquarters_location"] = {"include": [{"country": c} for c in f.countries]}
    if f.funding_series or f.funded_within_months:
        fund: dict = {}
        if f.funding_series:
            fund["series"] = f.funding_series
        if f.funded_within_months:
            fund["date"] = {"from": (now - timedelta(days=int(f.funded_within_months * 30.44))).date().isoformat()}
        body["funding"] = fund
    if f.hiring_roles:
        body["job_openings"] = {"job_title": {"include": f.hiring_roles}}
    if f.keywords:
        body["keywords"] = {"include": f.keywords, "match": "any"}
    if f.exclude_non_commercial:
        body["company_type"] = {"exclude": NON_COMMERCIAL_TYPES}
    return body


NON_COMMERCIAL_TYPES = ["educational", "educational institution", "government agency", "non profit"]
NON_COMMERCIAL_NAME = re.compile(r"\b(university|college|institute of technology|school|academy|government|ministry|"
                                 r"municipal|foundation|trust hospital)\b", re.I)


def run_hunter_discover(ctx, f: DiscoveryFilters) -> tuple[dict, list[dict]]:
    requested = [r for r in f.requested() if r != "nrr"]
    if not requested:
        return {"source": "hunter_discover", "ok": False,
                "error": "Hunter Discover needs at least one of: industry, size, location, funding, hiring, keywords"}, []
    body = hunter_body(f, clock.now())
    try:
        resp = with_retries(lambda: ctx.hunter.discover(body), ctx)
    except IntegrationError as e:
        return {"source": "hunter_discover", "ok": False, "error": f"{e.kind}: {e.message}"}, []
    applied = sorted({HUNTER_FILTER_NAMES[k] for k in (resp.get("meta", {}).get("filters") or {}) if k in HUNTER_FILTER_NAMES})
    ignored = [r for r in requested if r not in applied]
    report = {"source": "hunter_discover", "ok": True, "returned": len(resp.get("data", [])),
              "applied_filters": applied, "ignored_filters": ignored,
              "note": ("Hunter ignored " + ", ".join(ignored) + " (not available on your plan); those stay 'unknown' unless another source shows them.")
              if ignored else "All requested Hunter filters were applied."}
    out = []
    skipped_non_commercial = []
    for c in resp.get("data", []):
        d = normalize_domain(c.get("domain") or "")
        if not d:
            continue
        # Hunter's company-type tags are incomplete; also drop obvious institutions by name/domain.
        if f.exclude_non_commercial and (NON_COMMERCIAL_NAME.search(c.get("organization") or "")
                                         or re.search(r"\.(edu|ac|gov|gouv|mil)(\.[a-z]{2})?$", d)):
            skipped_non_commercial.append(c.get("organization") or d)
            continue
        out.append({"name": c.get("organization") or d, "domain": d, "source": "hunter_discover",
                    "provider_filters": applied,
                    "evidence": [{"source": "hunter_discover", "url": None, "title": "Hunter Discover",
                                  "excerpt": f"Returned by Hunter Discover with filters applied: {', '.join(applied) or 'none'}",
                                  "published_at": None, "signal": "provider_filter"}],
                    "signals": {}})
    if skipped_non_commercial:
        report["skipped_non_commercial"] = skipped_non_commercial
    return report, out


# ------------------------------------------------------------------ News / web search

def research_prompt(f: DiscoveryFilters, now: datetime) -> str:
    parts = []
    if f.industries:
        parts.append("in these industries: " + ", ".join(f.industries))
    if f.headcount:
        parts.append("with roughly these employee counts: " + ", ".join(f.headcount))
    if f.countries:
        parts.append("headquartered in: " + ", ".join(f.countries))
    if f.funding_series:
        parts.append("that announced a " + " or ".join(SERIES_LABEL[s] for s in f.funding_series) + " funding round")
    if f.funded_within_months:
        since = (now - timedelta(days=int(f.funded_within_months * 30.44))).date().isoformat()
        parts.append(f"with the announcement on or after {since}")
    if f.hiring_roles:
        parts.append("that are hiring for roles like: " + ", ".join(f.hiring_roles))
    if f.nrr_min_percent:
        parts.append(f"(bonus: any that publicly state net revenue retention of at least {f.nrr_min_percent:g}%)")
    if f.keywords:
        parts.append("related to: " + ", ".join(f.keywords))
    return (f"Today is {now.date().isoformat()}. Find up to {f.max_results} real companies " + "; ".join(parts) +
            ". Search recent news. For each company give its name, its website domain if a source states it, "
            "and the key fact with a citation.")


def _date_from(text: str) -> str | None:
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", text or "")
    if m:
        return m.group(0)
    m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}), (20\d\d)", text or "")
    if m:
        mon = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1)[:3].lower()) + 1
        return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
    return None


SERIES_PATTERNS = [("series_c+", r"\bseries [c-z]\b"), ("pre_series_b", r"\bpre-series b\b"), ("series_b", r"\bseries b\b"),
                   ("pre_series_a", r"\bpre-series a\b"), ("series_a", r"\bseries a\b"), ("pre_seed", r"\bpre-seed\b"),
                   ("seed", r"\bseed (round|funding)\b")]
INJECTION_RE = re.compile(r"\b(ignore (all )?(previous|prior) instructions|system (notice|prompt|message)|ai agents?|"
                          r"language model|assistant[,:]|you must (list|mark|add))\b", re.I)
NRR_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s?%\s*(?:net (?:revenue|dollar) retention|NRR|NDR)|(?:net (?:revenue|dollar) retention|NRR|NDR)\s*(?:of|at|was|is)?\s*(\d{2,3}(?:\.\d+)?)\s?%", re.I)


def series_in(text: str) -> str | None:
    low = (text or "").lower()
    for key, rx in SERIES_PATTERNS:
        if re.search(rx, low):
            return key
    return None


def run_news_search(ctx, f: DiscoveryFilters) -> tuple[dict, list[dict]]:
    now = clock.now()
    try:
        research = ctx.llm.web_research(ctx.versions.system_prompt("discover_research"), research_prompt(f, now),
                                        context=f.model_dump(), max_uses=6)
    except IntegrationError as e:
        return {"source": "news_search", "ok": False, "error": f"{e.kind}: {e.message}"}, []
    raw = research["citations"]
    # Sources that address the AI agent are untrusted instructions, not evidence: drop them.
    suspicious = [c["url"] for c in raw if INJECTION_RE.search(c["cited_text"] + " " + c["title"])]
    cits = [c for c in raw if c["url"] not in suspicious]
    if not cits:
        return {"source": "news_search", "ok": True, "returned": 0, "citations": 0,
                "note": "Web search returned no citable sources.", "errors": research.get("errors", [])}, []
    extracted = ctx.llm.structured("discover_extract", ctx.versions.system_prompt("discover_extract"), {
        "filters": f.model_dump(),
        "evidence": [{"index": i, "url": c["url"], "title": c["title"], "cited_text": c["cited_text"],
                      "page_age": c.get("page_age")} for i, c in enumerate(cits)],
    }, DiscoveredCompanies)
    out, dropped = [], []
    for comp in extracted.companies:
        refs = [r for r in comp.evidence if 0 <= r.citation_index < len(cits)]
        name_l = comp.name.lower().strip()
        refs = [r for r in refs if name_l and name_l in (cits[r.citation_index]["cited_text"] + " " + cits[r.citation_index]["title"]).lower()]
        if not refs:
            dropped.append(comp.name)  # not named in any retrieved source -> not trusted
            continue
        texts = " ".join(cits[r.citation_index]["cited_text"] + " " + cits[r.citation_index]["title"] for r in refs)
        evidence = [{"source": "news_search", "url": cits[r.citation_index]["url"], "title": cits[r.citation_index]["title"],
                     "excerpt": cits[r.citation_index]["cited_text"], "signal": r.signal,
                     "published_at": _date_from(str(cits[r.citation_index].get("page_age") or "")) or _date_from(cits[r.citation_index]["cited_text"])}
                    for r in refs]
        signals: dict = {}
        s = series_in(texts)
        if s:
            fdate = comp.funding_date if (comp.funding_date and comp.funding_date in texts) else None
            fdate = fdate or _date_from(texts) or next((e["published_at"] for e in evidence if e["published_at"]), None)
            signals["funding"] = {"series": s, "date": fdate, "basis": "stated in cited source"}
        m = NRR_RE.search(texts)
        if m:
            signals["nrr"] = {"percent": float(m.group(1) or m.group(2)), "text": m.group(0), "basis": "stated in cited source"}
        if re.search(r"\bhiring\b|\bjob opening|\bis looking for\b", texts, re.I):
            signals["hiring"] = {"text": next((e["excerpt"] for e in evidence if "hiring" in e["excerpt"].lower()), texts[:200])}
        domain = None
        if comp.domain:
            d = normalize_domain(comp.domain)
            if d and (d in texts.lower() or any(d in (e["url"] or "") for e in evidence)):
                domain = d
        out.append({"name": comp.name.strip(), "domain": domain, "source": "news_search", "provider_filters": [],
                    "evidence": evidence, "signals": signals})
    report = {"source": "news_search", "ok": True, "returned": len(out), "citations": len(cits),
              "dropped_unsupported": dropped, "suspicious_sources_ignored": sorted(set(suspicious)),
              "errors": research.get("errors", [])}
    return report, out


def resolve_domain_by_name(ctx, name: str) -> str | None:
    """Free Hunter Discover lookup by exact organization name. Never guesses."""
    try:
        resp = ctx.hunter.discover({"organization": {"name": [name]}, "limit": 3})
    except (IntegrationError, AttributeError):
        return None
    for c in resp.get("data", []):
        if (c.get("organization") or "").strip().lower() == name.strip().lower():
            return normalize_domain(c.get("domain") or "")
    return None


# ------------------------------------------------------------------ matching

def evaluate(f: DiscoveryFilters, cand: dict, now: datetime) -> tuple[dict, str]:
    pf = set(cand["provider_filters"])
    sig = cand["signals"]
    m: dict = {}

    def hunter_or_unknown(key, label):
        if key in pf:
            m[key] = {"status": "matched", "basis": f"Hunter Discover applied the {label} filter"}
        else:
            m[key] = {"status": "unknown", "basis": "no source confirms this"}

    if f.industries:
        hunter_or_unknown("industry", "industry")
    if f.headcount:
        hunter_or_unknown("headcount", "company size")
    if f.countries:
        hunter_or_unknown("location", "location")
    if f.funding_series or f.funded_within_months:
        fund = sig.get("funding")
        if "funding" in pf:
            m["funding"] = {"status": "matched", "basis": "Hunter Discover applied the funding filter"}
        elif fund:
            ok_series = (not f.funding_series) or fund["series"] in f.funding_series
            ok_date = True
            if f.funded_within_months and fund.get("date"):
                dt = datetime.fromisoformat(fund["date"]).replace(tzinfo=timezone.utc)
                ok_date = dt >= now - timedelta(days=int(f.funded_within_months * 30.44))
            elif f.funded_within_months and not fund.get("date"):
                ok_date = None
            label = f"{SERIES_LABEL[fund['series']]}" + (f" on {fund['date']}" if fund.get("date") else " (date not stated)")
            if ok_series and ok_date:
                m["funding"] = {"status": "matched", "basis": f"News: {label}"}
            elif ok_series is False or ok_date is False:
                m["funding"] = {"status": "not_matched", "basis": f"News: {label}"}
            else:
                m["funding"] = {"status": "unknown", "basis": f"News: {label}; date needed for the time window"}
        else:
            m["funding"] = {"status": "unknown", "basis": "no source states a funding round"}
    if f.hiring_roles:
        if "hiring" in pf:
            m["hiring"] = {"status": "matched", "basis": "Hunter Discover applied the job-openings filter"}
        elif sig.get("hiring") and any(r.lower() in sig["hiring"]["text"].lower() for r in f.hiring_roles):
            m["hiring"] = {"status": "matched", "basis": "News: " + sig["hiring"]["text"][:120]}
        else:
            m["hiring"] = {"status": "unknown", "basis": "no source shows a matching opening"}
    if f.nrr_min_percent:
        n = sig.get("nrr")
        if n:
            st = "matched" if n["percent"] >= f.nrr_min_percent else "not_matched"
            m["nrr"] = {"status": st, "basis": f"Stated: {n['text']}"}
        else:
            m["nrr"] = {"status": "unknown", "basis": "NRR not published (normal for private companies)"}
    if f.keywords:
        ev_text = " ".join(e["excerpt"] or "" for e in cand["evidence"]).lower()
        if "keywords" in pf or any(k.lower() in ev_text for k in f.keywords):
            m["keywords"] = {"status": "matched", "basis": "Hunter keyword filter or cited text"}
        else:
            m["keywords"] = {"status": "unknown", "basis": "not confirmed"}
    statuses = [v["status"] for v in m.values()]
    if "not_matched" in statuses:
        level = "excluded"
    elif not cand.get("domain"):
        level = "needs_domain"
    elif statuses and all(s == "matched" for s in statuses):
        level = "strong"
    else:
        level = "partial"
    return m, level


# ------------------------------------------------------------------ orchestration

def _merge(cands: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for c in cands:
        key = c["domain"] or "name:" + c["name"].lower()
        if key not in merged:
            # also merge a name-only entry into a domain entry with the same name
            same = next((k for k, v in merged.items() if v["name"].lower() == c["name"].lower()), None)
            key = same or key
        if key not in merged:
            merged[key] = {**c, "sources": [c["source"]]}
            continue
        t = merged[key]
        t["domain"] = t["domain"] or c["domain"]
        t["sources"] = sorted(set(t["sources"] + [c["source"]]))
        t["provider_filters"] = sorted(set(t["provider_filters"]) | set(c["provider_filters"]))
        t["evidence"] = t["evidence"] + c["evidence"]
        t["signals"] = {**c["signals"], **t["signals"]}
    return list(merged.values())


ORDER = {"strong": 0, "partial": 1, "needs_domain": 2, "excluded": 3}


def run_search(ctx, f: DiscoveryFilters) -> dict:
    now = clock.now()
    reports, cands = [], []
    if "hunter_discover" in f.sources:
        r, c = run_hunter_discover(ctx, f)
        reports.append(r)
        cands += c
    if "news_search" in f.sources:
        r, c = run_news_search(ctx, f)
        reports.append(r)
        cands += c
    merged = _merge(cands)
    for c in merged:
        if not c["domain"]:
            c["domain"] = resolve_domain_by_name(ctx, c["name"])
            if c["domain"]:
                c["evidence"].append({"source": "hunter_discover", "url": None, "title": "Hunter Discover",
                                      "excerpt": f"Domain resolved by exact company-name lookup: {c['name']} -> {c['domain']}",
                                      "published_at": None, "signal": "domain"})
        c["matches"], c["match_level"] = evaluate(f, c, now)
    merged.sort(key=lambda c: (ORDER[c["match_level"]], -sum(v["status"] == "matched" for v in c["matches"].values())))
    status = "completed" if all(r["ok"] for r in reports) else ("partial" if any(r["ok"] for r in reports) else "failed")
    sid = store.new_id("disc")
    ts = clock.iso(now)
    ctx.conn.execute("INSERT INTO discovery_searches(id,filters,sources,mode,status,source_reports,created_at) VALUES (?,?,?,?,?,?,?)",
                     (sid, f.model_dump_json(), json.dumps(f.sources), ctx.settings.mode, status, json.dumps(reports), ts))
    for c in merged:
        ctx.conn.execute(
            "INSERT INTO discovery_candidates(id,search_id,name,domain,sources,evidence,signals,matches,match_level,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (store.new_id("cand"), sid, c["name"], c["domain"], json.dumps(c["sources"]), json.dumps(c["evidence"]),
             json.dumps(c["signals"]), json.dumps(c["matches"]), c["match_level"], "new", ts))
    counts = {k: sum(c["match_level"] == k for c in merged) for k in ORDER}
    store.log_activity(ctx.conn, None, "discovery_search", status,
                       f"{len(merged)} candidates ({counts}); filters: {', '.join(f.requested()) or 'none'}",
                       tool="discovery", details={"reports": reports}, actor="human")
    return get_search(ctx, sid)


def get_search(ctx, sid: str) -> dict:
    s = ctx.conn.execute("SELECT * FROM discovery_searches WHERE id=?", (sid,)).fetchone()
    if not s:
        raise KeyError(sid)
    out = dict(s)
    for k in ("filters", "sources", "source_reports"):
        out[k] = json.loads(out[k])
    rows = ctx.conn.execute("SELECT * FROM discovery_candidates WHERE search_id=?", (sid,)).fetchall()
    cands = []
    for r in rows:
        d = dict(r)
        for k in ("sources", "evidence", "signals", "matches"):
            d[k] = json.loads(d[k])
        cands.append(d)
    cands.sort(key=lambda c: (ORDER[c["match_level"]], -sum(v["status"] == "matched" for v in c["matches"].values())))
    out["candidates"] = cands
    return out


def set_candidate_domain(ctx, cand_id: str, domain: str) -> dict:
    d = normalize_domain(domain)
    if not d:
        raise ValueError("not a valid public domain")
    r = ctx.conn.execute("SELECT * FROM discovery_candidates WHERE id=?", (cand_id,)).fetchone()
    if not r:
        raise KeyError(cand_id)
    s = get_search(ctx, r["search_id"])
    f = DiscoveryFilters(**s["filters"])
    cand = {"domain": d, "provider_filters": [], "signals": json.loads(r["signals"]), "evidence": json.loads(r["evidence"])}
    matches, level = evaluate(f, cand, clock.now())
    ctx.conn.execute("UPDATE discovery_candidates SET domain=?, matches=?, match_level=? WHERE id=?",
                     (d, json.dumps(matches), level, cand_id))
    store.log_activity(ctx.conn, None, "discovery_domain_set", "ok", f"{r['name']} -> {d} (entered by user)", actor="human")
    return get_search(ctx, r["search_id"])


def add_to_run(ctx, sid: str, candidate_ids: list[str], name: str | None = None) -> dict:
    from .outreach import create_campaign
    rows = [dict(r) for r in ctx.conn.execute(
        f"SELECT * FROM discovery_candidates WHERE search_id=? AND id IN ({','.join('?' * len(candidate_ids))})",
        (sid, *candidate_ids))] if candidate_ids else []
    ok = [r for r in rows if r["domain"] and r["match_level"] in ("strong", "partial") and r["status"] == "new"]
    skipped = [{"name": r["name"], "reason": "already added" if r["status"] == "added" else
                ("no domain yet" if not r["domain"] else f"match level {r['match_level']}")}
               for r in rows if r not in ok]
    if not ok:
        return {"campaign_id": None, "added": [], "skipped": skipped}
    res = create_campaign(ctx, name or f"Discovery {sid}", [r["domain"] for r in ok])
    for r in ok:
        ctx.conn.execute("UPDATE discovery_candidates SET status='added', campaign_id=? WHERE id=?", (res["campaign_id"], r["id"]))
    return {"campaign_id": res["campaign_id"], "added": res["domains"], "skipped": skipped}
