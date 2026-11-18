"""Company qualification: the LLM proposes levels + evidence IDs; code scores and decides.

points = weight * level / 2
QUALIFIED requires total >= 65, business_fit == 2, compliance_workload >= 1.
Level 0 always carries a zero_reason (unknown / explicit_negative / outside_window).
"""
from __future__ import annotations

from datetime import datetime

from .. import clock
from ..schemas import DIMENSIONS, QualificationProposal


class QStatus:
    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"
    LOW_PRIORITY = "LOW_PRIORITY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_RESEARCH = "NEEDS_RESEARCH"  # interim only


def _months_ago(published_at: str | None, now: datetime) -> float | None:
    if not published_at:
        return None
    try:
        dt = clock.parse(published_at if "T" in published_at else published_at + "T00:00:00+00:00")
    except ValueError:
        return None
    return (now - dt).days / 30.44


def score(proposal: QualificationProposal, evidence: dict[str, dict], rubric: dict, now: datetime | None = None) -> dict:
    now = now or clock.now()
    q = rubric["qualification"]
    weights = {d["id"]: d["weight"] for d in q["dimensions"]}
    win2, win1 = q["reason_now_window_months"]["level_2"], q["reason_now_window_months"]["level_1"]
    props = {d.dimension: d for d in proposal.dimensions}
    dims = {}
    for dim in DIMENSIONS:
        p = props.get(dim)
        notes = []
        if p is None:
            level, ids, zero, reason = 0, [], "unknown", "no proposal returned"
        else:
            level, reason = p.level, p.reason
            ids = [e for e in p.evidence_ids if e in evidence]
            dropped = [e for e in p.evidence_ids if e not in evidence]
            if dropped:
                notes.append(f"ignored unknown evidence IDs {dropped}")
            zero = p.zero_reason
            if level > 0 and not ids:
                notes.append(f"proposed level {level} had no valid evidence -> 0 (unknown)")
                level, zero = 0, "unknown"
            if level == 0 and zero == "explicit_negative" and not ids:
                notes.append("explicit_negative without evidence -> unknown")
                zero = "unknown"
            if level == 0 and zero is None:
                zero = "unknown"
            if dim == "reason_now" and level > 0:
                ages = [a for a in (_months_ago(evidence[e].get("published_at"), now) for e in ids) if a is not None]
                best = min(ages) if ages else None
                if best is None:
                    notes.append("reason_now signal undated -> 0")
                    level, zero = 0, "outside_window"
                elif level == 2 and best > win2:
                    level = 1 if best <= win1 else 0
                    zero = None if level else "outside_window"
                    notes.append(f"signal {best:.1f} months old; capped")
                elif level == 1 and best > win1:
                    level, zero = 0, "outside_window"
                    notes.append(f"signal {best:.1f} months old; outside 12-month window")
        if level > 0:
            zero = None
        dims[dim] = {"level": level, "points": weights[dim] * level / 2, "weight": weights[dim],
                     "evidence_ids": ids,
                     "zero_reason": zero, "reason": reason, "validation_notes": notes}
    total = sum(d["points"] for d in dims.values())

    disq_ids = [e for e in proposal.disqualifier_evidence_ids if e in evidence]
    disqualifier = proposal.disqualifier if (proposal.disqualifier != "none" and disq_ids) else "none"
    disq_note = None
    if proposal.disqualifier != "none" and not disq_ids:
        disq_note = f"disqualifier '{proposal.disqualifier}' ignored: no valid evidence"

    fit, work = dims["business_fit"]["level"], dims["compliance_workload"]["level"]
    if disqualifier != "none":
        status = QStatus.DISQUALIFIED
    elif total >= q["qualified_min_total"] and fit >= q["required_business_fit_level"] \
            and work >= q["required_min_compliance_workload_level"]:
        status = QStatus.QUALIFIED
    else:
        status = QStatus.NEEDS_RESEARCH

    # Optimistic bound: unknown dimensions could still reach 2 with more evidence.
    def improvable(d):
        return d["level"] < 2 and d["zero_reason"] != "explicit_negative"

    optimistic = sum(weights[k] * (2 if (d["level"] == 0 and d["zero_reason"] == "unknown") else d["level"]) / 2
                     for k, d in dims.items())
    fit_possible = fit == 2 or improvable(dims["business_fit"])
    work_possible = work >= 1 or dims["compliance_workload"]["zero_reason"] == "unknown"
    optimistic_fit = optimistic + (weights["business_fit"] / 2 if fit == 1 else 0)
    could_qualify = optimistic_fit >= q["qualified_min_total"] and fit_possible and work_possible
    gaps = [g.model_dump() for g in proposal.gaps]
    return {
        "dimensions": dims, "total": total, "status": status, "could_qualify": could_qualify,
        "optimistic_total": optimistic_fit, "disqualifier": disqualifier, "disqualifier_evidence_ids": disq_ids,
        "disqualifier_note": disq_note, "gaps": gaps,
    }


def final_status(result: dict, research_exhausted: bool, all_retrieval_failed: bool) -> str:
    if result["status"] in (QStatus.QUALIFIED, QStatus.DISQUALIFIED):
        return result["status"]
    if all_retrieval_failed:
        return QStatus.INSUFFICIENT_EVIDENCE  # retrieval failure is not negative evidence
    if not result["could_qualify"]:
        return QStatus.LOW_PRIORITY
    return QStatus.INSUFFICIENT_EVIDENCE if research_exhausted else QStatus.NEEDS_RESEARCH
