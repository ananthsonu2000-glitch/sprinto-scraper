"""Pain hypotheses: LLM proposes, code verifies and scores.

Each hypothesis scored /10: evidence 0-3, recipient 0-2, consequence 0-2,
capability 0-2, honest 0-1. Pass: total >= 7, evidence >= 2, honest == 1.
Two distinct passing hypotheses are required; we never manufacture a second.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from . import store
from .claims import DEFICIENCY, honest_wording, invented_specifics
from .schemas import PainOutput
from .store import Stage


def verify_hypothesis(h, evidence: dict, approved_caps: set[str], rubric: dict) -> dict:
    valid = [e for e in h.evidence_ids if e in evidence]
    ev_text = " ".join(evidence[e]["excerpt"] for e in valid)
    notes = []
    lv = h.levels
    ev_cap = 0 if not valid else (2 if len(valid) == 1 else 3)
    ev_level = min(lv.evidence, ev_cap)
    if ev_level < lv.evidence:
        notes.append(f"evidence level capped {lv.evidence}->{ev_level} ({len(valid)} valid evidence IDs)")
    cap_level = lv.capability if h.relevant_capability in approved_caps else 0
    if cap_level != lv.capability:
        notes.append(f"capability '{h.relevant_capability}' is not approved")
    text = f"{h.hypothesis} {h.possible_business_consequence} {h.role_relevance}"
    specifics = invented_specifics(text, ev_text)
    deficiency = DEFICIENCY.search(text)
    honest = 1 if (lv.honest == 1 and honest_wording(h.hypothesis) and not specifics and not deficiency) else 0
    if specifics:
        notes.append(f"unsupported specifics: {specifics}")
    if deficiency:
        notes.append(f"unsupported deficiency claim: '{deficiency.group(0)}'")
    if not honest_wording(h.hypothesis):
        notes.append("hypothesis not phrased tentatively")
    cons_level = lv.consequence if not specifics else 0
    breakdown = {"evidence": ev_level, "recipient": lv.recipient, "consequence": cons_level,
                 "capability": cap_level, "honest": honest}
    total = sum(breakdown.values())
    r = rubric["pain"]
    passed = (total >= r["pass_min_total"] and ev_level >= r["pass_min_evidence"] and honest == r["required_honest"]
              and h.relevant_capability in approved_caps)  # only approved capabilities may be pitched
    return {
        "observed_fact": h.observed_fact, "evidence_ids": valid, "hypothesis": h.hypothesis,
        "role_relevance": h.role_relevance, "possible_business_consequence": h.possible_business_consequence,
        "relevant_capability": h.relevant_capability, "score_breakdown": breakdown, "total": total,
        "passed": passed, "validation_notes": notes, "kind": "hypothesis (not a verified fact)",
    }


def _distinct(a: dict, b: dict) -> bool:
    if a["relevant_capability"] == b["relevant_capability"] and set(a["evidence_ids"]) == set(b["evidence_ids"]):
        return False
    return SequenceMatcher(None, a["hypothesis"].lower(), b["hypothesis"].lower()).ratio() < 0.8


def generate_pains(ctx, p: dict) -> dict:
    conn = ctx.conn
    evidence = store.evidence_map(conn, p["id"])
    caps = ctx.versions.capabilities["capabilities"]
    out = ctx.llm.structured("pain", ctx.versions.system_prompt("pain"), {
        "company": p["normalized_domain"],
        "contact": {"name": p["contact"]["name"], "title": p["contact"]["title"]},
        "qualification": {k: v["level"] for k, v in p["qualification_detail"]["dimensions"].items()},
        "profile": p["company_profile"],
        "approved_capabilities": [{"id": c["id"], "name": c["name"], "approved_claim": c["approved_claim"]} for c in caps],
        "evidence": [{"id": k, "field": v["field"], "excerpt": v["excerpt"], "published_at": v["published_at"]}
                     for k, v in evidence.items()],
    }, PainOutput)
    verified = [verify_hypothesis(h, evidence, ctx.versions.capability_ids(), ctx.versions.rubric) for h in out.hypotheses]
    passing = sorted([v for v in verified if v["passed"]], key=lambda v: -v["total"])
    chosen: list[dict] = []
    for v in passing:
        if all(_distinct(v, c) for c in chosen):
            chosen.append(v)
        if len(chosen) == ctx.versions.rubric["pain"]["required_count"]:
            break
    detail = {"candidates": verified}
    if len(chosen) < ctx.versions.rubric["pain"]["required_count"]:
        p = store.save_prospect(conn, p, pain_hypotheses=chosen, pain_detail=detail, stage=Stage.DONE,
                                terminal_status="INSUFFICIENT_PAIN_EVIDENCE")
        store.log_activity(conn, p["id"], "pain_hypotheses", "INSUFFICIENT_PAIN_EVIDENCE",
                           f"{len(chosen)} of 2 distinct evidence-backed hypotheses passed", campaign_id=p["campaign_id"])
        return p
    p = store.save_prospect(conn, p, pain_hypotheses=chosen, pain_detail=detail, stage=Stage.PAIN_READY)
    store.log_activity(conn, p["id"], "pain_hypotheses", "ok",
                       "; ".join(f"{c['relevant_capability']} ({c['total']}/10)" for c in chosen), campaign_id=p["campaign_id"])
    return p
