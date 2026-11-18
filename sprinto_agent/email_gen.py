"""Email drafting + separate evaluation + deterministic mandatory checks.

Pass requires evaluator score >= 8/10 AND every mandatory check. At most two
revisions after the initial draft; otherwise EMAIL_QA_FAILED.
"""
from __future__ import annotations

import re

from . import store
from .claims import DEFICIENCY, FALSE_THREAD, GUARANTEE, PRICING, TENTATIVE, invented_specifics
from .email_layout import compose
from .schemas import EmailDraft, EmailEvaluation, EmailParts
from .store import Stage


def _company_name(p: dict) -> str:
    v = (p["company_profile"].get("company_name") or {}).get("value")
    if isinstance(v, list):
        v = v[0]
    return v or p["normalized_domain"]


def mandatory_checks(draft: EmailDraft, p: dict, evidence: dict, approved_caps: set[str], rubric: dict) -> dict:
    body, subject = draft.body, draft.subject
    first = p["contact"]["first_name"]
    company = _company_name(p)
    words = len(body.split())
    lo, hi = rubric["email"]["hard_word_range"]
    all_ev_text = " ".join(v["excerpt"] for v in evidence.values())
    unsupported_claims = [c.text for c in draft.claims if not any(e in evidence for e in c.evidence_ids)]
    claims_not_in_evidence = [c.text for c in draft.claims
                              if not any(_overlap(c.text, evidence[e]["excerpt"]) for e in c.evidence_ids if e in evidence)]
    specifics = invented_specifics(body, all_ev_text)
    other_people = [n for n in _other_contact_names(p) if re.search(rf"\b{re.escape(n)}\b", body)]
    checks = {
        "correct_person_company": {
            "passed": bool(re.search(rf"\b{re.escape(first)}\b", body)) and (company.lower() in (body + subject).lower()) and not other_people,
            "detail": f"addresses {first}; mentions {company}" + (f"; wrong names {other_people}" if other_people else "")},
        "evidence_for_every_claim": {
            "passed": bool(draft.claims) and not unsupported_claims and not claims_not_in_evidence,
            "detail": f"{len(draft.claims)} claims; unsupported={unsupported_claims}; not matching evidence={claims_not_in_evidence}"},
        "hypotheses_tentative": {"passed": bool(TENTATIVE.search(body)), "detail": "uses tentative wording"},
        "no_invented_metrics_guarantees_urgency": {
            "passed": not specifics and not GUARANTEE.search(body) and not PRICING.search(body),
            "detail": f"specifics={specifics}"},
        "no_false_re_or_follow_up": {
            "passed": not re.match(r"^\s*(re|fwd?)\s*:", subject, re.I) and not FALSE_THREAD.search(body),
            "detail": "subject/body do not fake a prior thread"},
        "no_unsupported_deficiency_claims": {
            "passed": not DEFICIENCY.search(body), "detail": (DEFICIENCY.search(body) or [""])[0] if DEFICIENCY.search(body) else "none"},
        "approved_capability": {"passed": draft.capability_id in approved_caps, "detail": draft.capability_id},
        "length_and_single_question": {
            "passed": lo <= words <= hi and body.count("?") == 1,
            "detail": f"{words} words; {body.count('?')} question marks"},
    }
    return checks


def _overlap(claim: str, excerpt: str) -> bool:
    c = set(re.findall(r"[a-z0-9]+", claim.lower()))
    e = set(re.findall(r"[a-z0-9]+", excerpt.lower()))
    return bool(c) and len(c & e) / len(c) >= 0.6


def _other_contact_names(p: dict) -> list[str]:
    me = p["contact"]["name"]
    return [c["name"] for c in (p.get("contact_candidates") or []) if c.get("name") and c["name"] != me
            and c["first_name"] != p["contact"]["first_name"]]


def draft_email(ctx, p: dict) -> dict:
    conn = ctx.conn
    evidence = store.evidence_map(conn, p["id"])
    primary = p["pain_hypotheses"][0]
    cap = ctx.versions.capability(primary["relevant_capability"])
    facts = [{"evidence_id": e, "excerpt": evidence[e]["excerpt"], "source_url": evidence[e]["source_url"]}
             for e in primary["evidence_ids"] if e in evidence]
    other_facts = [{"evidence_id": k, "excerpt": v["excerpt"]} for k, v in evidence.items()
                   if v["field"] in ("product", "company_name", "customer_segments") and k not in primary["evidence_ids"]][:4]
    base_ctx = {
        "company_name": _company_name(p), "contact_first_name": p["contact"]["first_name"],
        "contact_title": p["contact"]["title"], "primary_hypothesis": primary,
        "facts": facts + other_facts,
        "capability": {"id": cap["id"], "approved_claim": cap["approved_claim"], "short_claim": cap["short_claim"]},
        "sender_name": ctx.settings.sender_display_name, "feedback": [],
    }
    attempts = []
    max_rev = ctx.versions.rubric["email"]["max_revisions"]
    for i in range(max_rev + 1):
        parts = ctx.llm.structured("draft", ctx.versions.system_prompt("draft"), base_ctx, EmailParts)
        # Code owns the layout: greeting / opening / paragraphs / question / sign-off, blank lines between.
        draft = EmailDraft(subject=parts.subject.strip(),
                           body=compose(parts.greeting, parts.opening, parts.paragraphs, parts.question,
                                        ctx.settings.sender_display_name),
                           claims=parts.claims, capability_id=parts.capability_id)
        ev = ctx.llm.structured("evaluate_email", ctx.versions.system_prompt("evaluate_email"), {
            "draft": {"subject": draft.subject, "body": draft.body},
            "contact": {"first_name": p["contact"]["first_name"], "title": p["contact"]["title"]},
            "company": _company_name(p), "facts": facts}, EmailEvaluation)
        checks = mandatory_checks(draft, p, evidence, ctx.versions.capability_ids(), ctx.versions.rubric)
        score = ev.specific_hook + ev.useful_hypothesis + ev.relevant_value + ev.simple_next_step + ev.clarity
        failed = [k for k, v in checks.items() if not v["passed"]]
        passed = score >= ctx.versions.rubric["email"]["pass_min_total"] and not failed
        attempts.append({"attempt": i, "subject": draft.subject, "body": draft.body,
                         "claims": [c.model_dump() for c in draft.claims], "capability_id": draft.capability_id,
                         "evaluation": ev.model_dump(), "score": score, "checks": checks, "passed": passed})
        if passed:
            qa = {"passed": True, "score": score, "checks": checks, "evaluation": ev.model_dump(),
                  "revisions": i, "attempts": attempts}
            p = store.save_prospect(conn, p, draft=attempts[-1], email_qa=qa, stage=Stage.DRAFT_READY)
            store.log_activity(conn, p["id"], "email_qa", "passed", f"score {score}/10 after {i} revision(s)",
                               campaign_id=p["campaign_id"])
            return p
        base_ctx["feedback"] = ev.issues + [f"mandatory check failed: {k} ({checks[k]['detail']})" for k in failed]
    qa = {"passed": False, "score": attempts[-1]["score"], "checks": attempts[-1]["checks"],
          "revisions": max_rev, "attempts": attempts}
    p = store.save_prospect(conn, p, draft=attempts[-1], email_qa=qa, stage=Stage.DONE, terminal_status="EMAIL_QA_FAILED")
    store.log_activity(conn, p["id"], "email_qa", "EMAIL_QA_FAILED",
                       f"failed after {max_rev} revisions: {base_ctx['feedback'][:3]}", campaign_id=p["campaign_id"])
    return p
