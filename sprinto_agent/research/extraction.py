"""Structured extraction with evidence validation.

The LLM proposes field values with verbatim excerpts. Code:
  * validates every excerpt against the retrieved page text (whitespace-normalised),
  * creates evidence records only for validated excerpts,
  * downgrades unsupported fields to unknown,
  * enforces the "never infer" rules (funding stage, negatives from absence),
  * preserves precise compliance wording and conflicting claims across rounds.
"""
from __future__ import annotations

import re

from .. import store
from ..schemas import FIELD_NAMES, LIST_FIELDS, ExtractionOutput

WS = re.compile(r"\s+")
FUNDING_WORDS = re.compile(r"\b(series [a-f]|seed|pre-seed|funding|raised|ipo|bootstrapped|venture)\b", re.I)
NEGATIVE_COMPLIANCE = re.compile(r"\b(not|no|non-?|lack|lacks|without|missing)\b", re.I)
MAX_PAGE_CHARS = 20000


def norm(s: str) -> str:
    return WS.sub(" ", s or "").strip()


def excerpt_in(excerpt: str, text: str) -> bool:
    e = norm(excerpt)
    return len(e) >= 12 and e in norm(text)


def empty_profile(domain: str) -> dict:
    prof = {f: {"value": None, "evidence_ids": [], "status": "unknown"} for f in FIELD_NAMES}
    prof["domain"] = {"value": domain, "evidence_ids": [], "status": "observed", "note": "input domain"}
    return prof


def extraction_context(domain: str, pages: list[dict]) -> dict:
    ok = [p for p in pages if p["status"] == "ok"]
    return {
        "domain": domain,
        "fields": list(FIELD_NAMES),
        "pages": [{"url": p["final_url"] or p["url"], "title": p["title"], "text": (p["text"] or "")[:MAX_PAGE_CHARS],
                   "is_homepage": i == 0 and p.get("research_round", 0) == 0} for i, p in enumerate(ok)],
    }


def apply_extraction(conn, prospect: dict, pages: list[dict], out: ExtractionOutput, profile: dict) -> tuple[dict, list[str]]:
    """Validate the LLM's proposals and merge them into `profile`. Returns (profile, notes)."""
    notes: list[str] = []
    by_url = {}
    for p in pages:
        if p["status"] == "ok":
            by_url[p["url"]] = p
            if p["final_url"]:
                by_url[p["final_url"]] = p
    for f in out.fields:
        if f.name == "domain":
            continue
        valid_ids: list[str] = []
        for c in f.citations:
            page = by_url.get(c.url)
            if page is None:
                notes.append(f"{f.name}: citation URL not among fetched pages ({c.url}) — dropped")
                continue
            if not excerpt_in(c.excerpt, page["text"]):
                notes.append(f"{f.name}: excerpt not found verbatim on {c.url} — dropped")
                continue
            valid_ids.append(store.add_evidence(conn, prospect["id"], page, norm(c.excerpt), f.name, c.published_at))
        value, status = f.value, f.status
        if status != "unknown" and not valid_ids:
            if value is not None:
                notes.append(f"{f.name}: no validated evidence — set to unknown")
            value, status = None, "unknown"
        if status == "unknown":
            value = None
        excerpts = [r["excerpt"] for r in conn.execute(
            f"SELECT excerpt FROM evidence WHERE id IN ({','.join('?' * len(valid_ids))})", valid_ids)] if valid_ids else []
        joined = " ".join(excerpts)
        # Never infer funding stage from appearance/product: must be stated.
        if f.name == "funding_stage" and value is not None and not FUNDING_WORDS.search(joined):
            notes.append("funding_stage: not explicitly stated in evidence — kept unknown")
            value, status, valid_ids = None, "unknown", []
        # Preserve exact compliance wording; no negative claims unless verbatim.
        if f.name == "compliance_claims" and value is not None:
            vals = value if isinstance(value, list) else [value]
            kept = [v for v in vals if v and v in joined and (not NEGATIVE_COMPLIANCE.search(v) or v in joined)]
            dropped = [v for v in vals if v not in kept]
            if dropped:
                notes.append(f"compliance_claims: dropped non-verbatim wording {dropped}")
            value = kept or None
            if value is None:
                status, valid_ids = "unknown", []
        if f.name in LIST_FIELDS and value is not None and not isinstance(value, list):
            value = [value]
        _merge(profile, f.name, value, status, valid_ids)
    return profile, notes


def _merge(profile: dict, name: str, value, status: str, ids: list[str]) -> None:
    cur = profile.get(name) or {"value": None, "evidence_ids": [], "status": "unknown"}
    if value is None:
        return  # unknown never overwrites known
    all_ids = list(dict.fromkeys(cur["evidence_ids"] + ids))
    if cur["value"] is None:
        profile[name] = {"value": value, "evidence_ids": all_ids, "status": status}
    elif name in LIST_FIELDS:
        merged = list(dict.fromkeys((cur["value"] or []) + (value or [])))
        profile[name] = {"value": merged, "evidence_ids": all_ids, "status": cur["status"] if cur["status"] != "unknown" else status}
    elif cur["value"] != value:
        old = cur["value"] if isinstance(cur["value"], list) else [cur["value"]]
        new = value if isinstance(value, list) else [value]
        both = list(dict.fromkeys(old + new))
        profile[name] = {"value": both if len(both) > 1 else both[0], "evidence_ids": all_ids,
                         "status": "conflicting" if len(both) > 1 else status,
                         "note": "conflicting first-party claims preserved" if len(both) > 1 else None}
    else:
        profile[name] = {**cur, "evidence_ids": all_ids}
