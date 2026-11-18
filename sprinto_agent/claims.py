"""Deterministic claim-safety checks shared by pain hypotheses and email QA."""
from __future__ import annotations

import re

TENTATIVE = re.compile(r"\b(may|might|could|possibly|perhaps|likely|if)\b", re.I)
OVERCLAIM = re.compile(r"\b(definitely|certainly|clearly struggling|you are struggling|you're struggling|obviously|guarantee[sd]?)\b", re.I)
# invented specifics: allowed only if the same term appears in cited evidence
INVENTED_SPECIFICS = [
    (re.compile(r"\b\d+(\.\d+)?\s?%"), "percentage"),
    (re.compile(r"\b\d+\s?(hours?|hrs|days|weeks|months)\b", re.I), "time quantity"),
    (re.compile(r"\$\s?\d|\b\d+\s?(k|m|million|billion)\b", re.I), "money amount"),
    (re.compile(r"\b(manual(ly)?|spreadsheets?)\b", re.I), "manual-process assumption"),
    (re.compile(r"\b(revenue loss|lost revenue|losing revenue|lost deals|losing deals)\b", re.I), "revenue loss"),
    (re.compile(r"\b(urgent|urgently|asap|right away|act now|limited time|deadline)\b", re.I), "urgency"),
    (re.compile(r"\b(frustrat\w*|unhappy|dissatisf\w*|pain with (vanta|drata|secureframe))\b", re.I), "incumbent dissatisfaction"),
    (re.compile(r"\b(save[sd]? (you )?(time|\d)|cut .* in half|faster than)\b", re.I), "savings claim"),
]
DEFICIENCY = re.compile(
    r"\b(not|aren't|isn't|lack(s|ing)?|without|missing|no)\s+(an?\s+)?(soc ?2|iso ?27001|hipaa|compliance|compliant|certif\w*|security program)"
    r"|\bnon-?compliant\b|\bfail(ed|ing)?\s+(your|the|an)\s+audit\b|\bgaps? in your (security|compliance)\b", re.I)
FALSE_THREAD = re.compile(r"\b(following up|follow up on my|circling back|as promised|per our (conversation|call)|as we discussed|bumping this)\b", re.I)
GUARANTEE = re.compile(r"\b(guarantee\w*|promise[sd]?|ensure you pass|100%|certif(y|ied) you)\b", re.I)
PRICING = re.compile(r"\b(price|pricing|cost[s]?|discount|\$\d)\b", re.I)


def invented_specifics(text: str, evidence_text: str) -> list[str]:
    hits = []
    for rx, label in INVENTED_SPECIFICS:
        for m in rx.finditer(text):
            if m.group(0).lower() not in evidence_text.lower():
                hits.append(f"{label}: '{m.group(0)}'")
    return hits


def honest_wording(text: str) -> bool:
    return bool(TENTATIVE.search(text)) and not OVERCLAIM.search(text)
