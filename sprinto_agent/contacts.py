"""Contact selection over Hunter Domain Search candidates (deterministic scoring).

Every candidate is scored 0-2 on five criteria; pass requires >= 8/10 with company
association > 0 and ownership > 0. Among passing candidates the role preference
order is applied (compliance/GRC > security > engineering leader > technical
founder/CEO in a supported founder-led context). Nothing is invented: name, title
and email come only from Hunter; a missing email does not block (demo delivery).
"""
from __future__ import annotations

import re
from datetime import datetime

from . import clock, store
from .store import Stage
from .tools import run_tool

GRC = re.compile(r"\b(compliance|grc|governance|risk|audit|privacy|trust)\b", re.I)
SECURITY = re.compile(r"\b(ciso|security|infosec|information security)\b", re.I)
ENG_LEADER = re.compile(r"\b(cto|chief technology|vp,? engineering|vp of engineering|head of engineering|engineering director|director of engineering|head of infrastructure|vp,? infrastructure|devops lead|head of platform)\b", re.I)
FOUNDER = re.compile(r"\b(founder|co-founder|ceo|chief executive)\b", re.I)
TECH_FOUNDER = re.compile(r"\b(technical|cto|engineer)\b", re.I)
ADJACENT = re.compile(r"\b(operations|coo|legal|counsel|product|it manager|it director|engineering manager|engineer|architect|sre)\b", re.I)
SENIOR = re.compile(r"\b(chief|ciso|cto|ceo|vp|vice president|head|director|founder|co-founder)\b", re.I)
MID = re.compile(r"\b(manager|lead|principal|senior|sr\.?)\b", re.I)


def role_tier(title: str, founder_led: bool) -> tuple[int, str]:
    t = title or ""
    if GRC.search(t):
        return 0, "compliance_grc"
    if SECURITY.search(t):
        return 1, "security"
    if ENG_LEADER.search(t):
        return 2, "engineering_leader"
    if FOUNDER.search(t) and (founder_led or TECH_FOUNDER.search(t)):
        return 3, "technical_founder_ceo"
    return 9, "other"


def _months_since(date_s: str | None, now: datetime) -> float | None:
    if not date_s:
        return None
    try:
        dt = clock.parse(date_s if "T" in date_s else date_s + "T00:00:00+00:00")
    except ValueError:
        return None
    return (now - dt).days / 30.44


def score_candidate(c: dict, domain: str, need: dict, founder_led: bool, recent_months: int, now=None) -> dict:
    now = now or clock.now()
    first, last, title = (c.get("first_name") or "").strip(), (c.get("last_name") or "").strip(), (c.get("position") or "").strip()
    sources = c.get("sources") or []
    reasons = []
    # 1. company association
    first_party = [s for s in sources if (s.get("domain") or "").lower().removeprefix("www.") == domain and s.get("still_on_page", True)]
    recent = [s for s in sources if (m := _months_since(s.get("last_seen_on"), now)) is not None and m <= recent_months and s.get("still_on_page", True)]
    if not (first or last or title):
        assoc = 0
    elif first_party or recent:
        assoc = 2
        reasons.append("first-party or recent source")
    else:
        assoc = 1
        reasons.append("provider association only")
    # 2. functional ownership
    tier, tier_name = role_tier(title, founder_led)
    if tier <= 2 or tier_name == "technical_founder_ceo":
        own = 2
    elif ADJACENT.search(title) or FOUNDER.search(title):
        own = 1
    else:
        own = 0
    # 3. ability to drive action
    ability = 2 if SENIOR.search(title) or (c.get("seniority") == "executive") else (1 if MID.search(title) or c.get("seniority") == "senior" else 0)
    # 4. need match
    if tier <= 1:  # compliance/GRC or security owner
        need_m = 2 if need.get("compliance_evidence") else 1
    elif tier == 2:  # engineering leader: explicit owner only when no security owner exists
        need_m = 2 if (need.get("compliance_evidence") and not need.get("security_owner_found")) else 1
    elif tier == 3:
        need_m = 1
    else:
        need_m = 0
    # 5. identity quality
    if first and last and title and sources:
        ident = 2
    elif first and (title or sources):
        ident = 1
    else:
        ident = 0
    total = assoc + own + ability + need_m + ident
    passed = total >= 8 and assoc > 0 and own > 0
    return {
        "name": f"{first} {last}".strip(), "first_name": first, "last_name": last, "title": title,
        "email_research_only": c.get("value"), "hunter_confidence": c.get("confidence"),
        "seniority": c.get("seniority"), "department": c.get("department"),
        "source_count": len(sources), "sources": sources[:3],
        "score_breakdown": {"company_association": assoc, "functional_ownership": own, "ability_to_drive_action": ability,
                            "need_match": need_m, "identity_quality": ident},
        "total": total, "passed": passed, "role_tier": tier, "role": tier_name, "notes": reasons,
    }


def select_contact(ctx, p: dict) -> dict:
    conn = ctx.conn
    res = run_tool(ctx, "search_hunter_contacts", {"prospect_id": p["id"], "domain": p["normalized_domain"]})
    p = store.get_prospect(conn, p["id"])
    if not res.ok:
        if res.error_kind in ("quota", "auth", "setup_required"):
            store.log_activity(conn, p["id"], "contact_lookup", "paused",
                               f"Hunter unavailable ({res.error_kind}): {res.error}", campaign_id=p["campaign_id"])
            return store.save_prospect(conn, p, stage=Stage.WAITING_INTEGRATION, paused_stage=Stage.QUALIFIED,
                                       last_error=f"hunter {res.error_kind}: {res.error}")
        store.open_escalation(conn, p, "CONTACT_LOOKUP_FAILED", f"{res.error_kind}: {res.error}")
        return store.get_prospect(conn, p["id"])

    rubric = ctx.versions.rubric["contact"]
    prof = p["company_profile"]
    qd = p["qualification_detail"]["dimensions"]
    emp = (prof.get("employee_count") or {}).get("value")
    try:
        emp_n = int(str(emp).replace(",", "").replace("+", "")) if emp and not isinstance(emp, list) else None
    except ValueError:
        emp_n = None
    founder_led = emp_n is not None and emp_n <= rubric["founder_led_max_employees"]
    candidates_raw = res.data.get("emails") or []
    has_security_owner = any(role_tier(c.get("position") or "", founder_led)[0] <= 1 for c in candidates_raw)
    need = {"compliance_evidence": qd["compliance_workload"]["level"] >= 1,
            "security_owner_found": has_security_owner}
    scored = [score_candidate(c, p["normalized_domain"], need, founder_led, rubric["recent_source_months"])
              for c in candidates_raw]
    passing = [s for s in scored if s["passed"] and s["role_tier"] < 9]
    passing.sort(key=lambda s: (s["role_tier"], -s["total"], -(s["hunter_confidence"] or 0)))
    stored = sorted(scored, key=lambda s: (not s["passed"], s["role_tier"], -s["total"]))
    if not passing:
        p = store.save_prospect(conn, p, contact_candidates=stored, stage=Stage.DONE,
                                terminal_status="NO_SUITABLE_CONTACT")
        store.log_activity(conn, p["id"], "contact_selection", "NO_SUITABLE_CONTACT",
                           f"{len(scored)} Hunter candidates; none met >=8/10 with association and ownership > 0",
                           campaign_id=p["campaign_id"])
        return p
    best = passing[0]
    best["selection_reason"] = (f"{best['name']} ({best['title']}): role preference '{best['role']}', "
                                f"score {best['total']}/10 — {best['score_breakdown']}")
    p = store.save_prospect(conn, p, contact=best, contact_score=best["total"], contact_candidates=stored,
                            stage=Stage.CONTACT_SELECTED)
    store.log_activity(conn, p["id"], "contact_selection", "selected", best["selection_reason"], campaign_id=p["campaign_id"])
    return p
