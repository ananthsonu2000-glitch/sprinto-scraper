"""Bounded research loop.

Initial research: homepage + up to 3 relevant pages.
Then up to 3 additional rounds, at most 2 new pages per round, each targeting a
named evidence gap. Stops when qualified, clearly poor fit, disqualified, or limits
are reached. State is persisted after every round, so a crash resumes mid-loop.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .. import clock, store
from ..schemas import ExtractionOutput, NextPagesProposal, QualificationProposal
from ..store import Stage
from ..tools import run_tool
from .extraction import apply_extraction, empty_profile, extraction_context
from .qualification import QStatus, final_status, score
from .url_guard import canonical_url, same_site

ASSET_RE = re.compile(r"\.(pdf|png|jpe?g|gif|svg|zip|mp4|webp|ico|css|js|xml)(\?|$)", re.I)


def _keyword_score(url: str, text: str, keywords: list[str]) -> int:
    path = urlsplit(url).path.lower()
    t = (text or "").lower()
    return sum(3 for k in keywords if k in path) + sum(1 for k in keywords if k in t)


def candidate_urls(domain: str, pages: list[dict], policy: dict) -> list[dict]:
    fetched = {canonical_url(p["url"]) for p in pages} | {canonical_url(p["final_url"]) for p in pages if p["final_url"]}
    # treat www/apex as the same page too
    fetched |= {u.replace("://www.", "://", 1) for u in fetched}
    cands: dict[str, str] = {}
    for p in pages:
        if p["status"] != "ok":
            continue
        for l in p["links"]:
            u = canonical_url(l["url"].split("?")[0])
            if same_site(u, domain) and not ASSET_RE.search(u) and u not in fetched \
                    and u.replace("://www.", "://", 1) not in fetched:
                cands.setdefault(u, l.get("text", ""))
    base = f"https://{domain}"
    for path in policy["research"]["guess_paths"]:
        u = canonical_url(base + path)
        if u not in fetched:
            cands.setdefault(u, "(common path guess)")
    return [{"url": u, "text": t} for u, t in cands.items()]


# Initial page mix: one page per category, most valuable first; index pages beat deep pages.
PAGE_CATEGORIES = [
    ("security/trust", ("security", "trust", "compliance")),
    ("customers", ("customers", "customer", "case-stud", "case_stud")),
    ("company/careers", ("careers", "jobs", "about", "newsroom", "press", "company")),
    ("product", ("product", "platform", "integrations", "solutions")),
]


def select_initial_pages(domain: str, pages: list[dict], policy: dict) -> list[tuple[str, str]]:
    """Returns [(url, reason)] — at most `initial_extra_pages`, one per category."""
    n = policy["research"]["initial_extra_pages"]
    cands = candidate_urls(domain, pages, policy)
    linked = [c for c in cands if c["text"] != "(common path guess)"]
    guesses = [c for c in cands if c["text"] == "(common path guess)"]
    picked: list[tuple[str, str]] = []

    def depth(u):
        parts = urlsplit(u)
        path = parts.path.strip("/")
        sub = 0 if (parts.hostname or "").split(".")[0] in ("trust", "security", "compliance") else 1
        return (sub, path.count("/") + (1 if path else 0), len(path))  # dedicated trust subdomain first

    for label, words in PAGE_CATEGORIES:
        if len(picked) >= n:
            break
        # match on path AND subdomain, e.g. trust.example.com or security.example.com
        match = [c for c in linked
                 if any(w in urlsplit(c["url"]).path.lower() or (urlsplit(c["url"]).hostname or "").startswith(w + ".")
                        for w in words)]
        if match:
            best = min(match, key=lambda c: depth(c["url"]))
            picked.append((best["url"], f"initial: {label} page (linked)"))
        elif label == "security/trust":
            g = next((c for c in guesses if urlsplit(c["url"]).path in ("/security", "/trust")), None)
            if g:
                picked.append((g["url"], "initial: security/trust page (common path; not linked from homepage)"))
    if len(picked) < n:
        chosen = {u for u, _ in picked}
        rest = sorted((c for c in linked if c["url"] not in chosen),
                      key=lambda c: -_keyword_score(c["url"], c["text"], policy["research"]["page_keywords"]))
        for c in rest:
            if len(picked) >= n or _keyword_score(c["url"], c["text"], policy["research"]["page_keywords"]) <= 0:
                break
            picked.append((c["url"], "initial: relevant page"))
    return picked


def _extracted_ids(p: dict) -> set[str]:
    return {pid for entry in p["research_log"] for pid in entry.get("extracted", [])}


def _log_round(p: dict, entry: dict) -> list:
    return p["research_log"] + [{**entry, "at": clock.iso(clock.now())}]


def research_prospect(ctx, p: dict) -> dict:
    conn = ctx.conn
    pol = ctx.policy["research"]
    domain = p["normalized_domain"]
    pages = store.list_pages(conn, p["id"])

    if not pages:
        if p["stage"] != Stage.RESEARCHING:
            p = store.save_prospect(conn, p, stage=Stage.RESEARCHING)
        home = run_tool(ctx, "fetch_website_page", {"prospect_id": p["id"], "url": f"https://{domain}/",
                                                    "research_round": 0, "selection_reason": "homepage"})
        if not home.ok:
            run_tool(ctx, "fetch_website_page", {"prospect_id": p["id"], "url": f"https://www.{domain}/",
                                                 "research_round": 0, "selection_reason": "homepage (www fallback)"})
        pages = store.list_pages(conn, p["id"])
        home_page = next((pg for pg in pages if pg["status"] == "ok"), None)
        picked = []
        if home_page:
            for u, why in select_initial_pages(domain, pages, ctx.policy):
                picked.append(u)
                run_tool(ctx, "fetch_website_page", {"prospect_id": p["id"], "url": u, "research_round": 0,
                                                     "selection_reason": why})
        p = store.get_prospect(conn, p["id"])
        p = store.save_prospect(conn, p, research_log=_log_round(p, {"round": 0, "fetched": ["/"] + picked,
                                                                     "reason": "initial research"}))

    while True:
        p = store.get_prospect(conn, p["id"])
        pages = store.list_pages(conn, p["id"])
        profile = p["company_profile"] or empty_profile(domain)
        if not profile:
            profile = empty_profile(domain)
        new_pages = [pg for pg in pages if pg["status"] == "ok" and pg["id"] not in _extracted_ids(p)]
        notes: list[str] = []
        if new_pages:
            out = ctx.llm.structured("extract", ctx.versions.system_prompt("extract"),
                                     extraction_context(domain, new_pages), ExtractionOutput)
            profile, notes = apply_extraction(conn, p, new_pages, out, profile)
        evidence = store.evidence_map(conn, p["id"])
        failures = [pg["url"] for pg in pages if pg["status"] != "ok"]
        q_ctx = {
            "domain": domain, "today": clock.now().date().isoformat(),
            "rubric": ctx.versions.rubric["qualification"]["dimensions"],
            "profile": profile,
            "retrieval_failures_not_negative_evidence": failures,
            "evidence": [{"id": k, "field": v["field"], "excerpt": v["excerpt"], "source_url": v["source_url"],
                          "published_at": v["published_at"]} for k, v in evidence.items()],
        }
        proposal = ctx.llm.structured("qualify", ctx.versions.system_prompt("qualify"), q_ctx, QualificationProposal)
        result = score(proposal, evidence, ctx.versions.rubric)
        rounds = p["research_rounds"]
        exhausted = rounds >= pol["max_additional_rounds"] or len(pages) >= pol["max_pages_per_domain"]
        all_failed = not any(pg["status"] == "ok" for pg in pages)
        status = final_status(result, exhausted, all_failed)
        result["extraction_notes"] = notes
        p = store.save_prospect(
            conn, p, company_profile=profile, evidence_ids=list(evidence.keys()),
            qualification_score=result["total"], qualification_status=status, qualification_detail=result,
            research_log=_log_round(p, {"round": rounds, "extracted": [pg["id"] for pg in new_pages],
                                        "total": result["total"], "status": status}))
        if status != QStatus.NEEDS_RESEARCH:
            return _finish(ctx, p, status, result)

        cands = candidate_urls(domain, pages, ctx.policy)
        nxt = ctx.llm.structured("next_pages", ctx.versions.system_prompt("next_pages"), {
            "domain": domain, "gaps": result["gaps"], "candidates": cands[:60],
            "fetched": [pg["url"] for pg in pages],
            "budget": {"rounds_left": pol["max_additional_rounds"] - rounds,
                       "pages_left": pol["max_pages_per_domain"] - len(pages)},
        }, NextPagesProposal)
        allowed = {c["url"] for c in cands}
        chosen = [u for u in dict.fromkeys(nxt.urls) if u in allowed][: pol["max_new_pages_per_round"]]
        if not chosen:
            status = final_status(result, True, all_failed)
            p = store.save_prospect(conn, p, qualification_status=status,
                                    research_log=_log_round(p, {"round": rounds + 1, "fetched": [],
                                                                "reason": "no useful page for remaining gap; stopping"}))
            return _finish(ctx, p, status, result)
        for u in chosen:
            run_tool(ctx, "fetch_website_page", {"prospect_id": p["id"], "url": u, "research_round": rounds + 1,
                                                 "selection_reason": f"gap {nxt.gap_dimension}: {nxt.gap_description}"[:200]})
        p = store.get_prospect(conn, p["id"])
        p = store.save_prospect(conn, p, research_rounds=rounds + 1,
                                research_log=_log_round(p, {"round": rounds + 1, "fetched": chosen,
                                                            "gap": nxt.gap_dimension, "reason": nxt.reason[:200]}))


def _finish(ctx, p, status, result) -> dict:
    reason = f"score {result['total']:.0f}/100; " + ", ".join(
        f"{k}={v['level']}" + (f"({v['zero_reason']})" if v["level"] == 0 else "") for k, v in result["dimensions"].items())
    if status == QStatus.QUALIFIED:
        p = store.save_prospect(ctx.conn, p, stage=Stage.QUALIFIED)
    else:
        if status == QStatus.DISQUALIFIED:
            reason = f"{result['disqualifier']}; " + reason
        p = store.save_prospect(ctx.conn, p, stage=Stage.DONE, terminal_status=status)
    store.log_activity(ctx.conn, p["id"], "qualification", status, reason, campaign_id=p["campaign_id"])
    return p
