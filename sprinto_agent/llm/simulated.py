"""SIMULATED LLM — deterministic, rule-based stand-in for Claude.

Used only in SIMULATION mode and tests. It returns the same schemas as the real
model so the whole pipeline (including all code-side validation) runs offline.
Its outputs are labelled "simulated" everywhere they surface. It is intentionally
simple; it is not a quality benchmark for the real model.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..schemas import (
    Citation, Claim, DimensionProposal, EmailDraft, EmailEvaluation, EvidenceGap, ExtractionOutput,
    FieldExtraction, InfoAnswer, NextPagesProposal, PainLevels, PainOutput, PainProposal,
    QualificationProposal, ReplyClassification,
)

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(r"(20\d\d)-(\d\d)-(\d\d)|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (?:(\d{1,2}), )?(20\d\d)")
SOFTWARE_RE = re.compile(r"\b(platform|software|SaaS|cloud|app|API|web-based|online service)\b", re.I)
COMPLIANCE_RE = re.compile(
    r"\b(SOC 2(?: Type (?:II|I|1|2))?(?: report| ready| compliant| certified| audit| attestation)?"
    r"|ISO(?:/IEC)? 27001(?: certified| certification| compliant)?|HIPAA(?: compliant)?|GDPR(?: compliant)?"
    r"|PCI DSS(?: Level 1)?)")
FRAMEWORK_KEYS = {"SOC 2": r"SOC 2", "ISO 27001": r"27001", "HIPAA": r"HIPAA", "PCI": r"PCI", "GDPR": r"GDPR"}

RULES = [
    ("product", re.compile(r"\b(platform|software|SaaS|app|API|bakery|bakes|cafe|restaurant|compliance automation)\b", re.I)),
    ("business_model", re.compile(r"\b(SaaS|cloud-based|cloud platform|subscription|bakery|retail store)\b", re.I)),
    ("customer_segments", re.compile(r"\b(enterprises?|healthcare|hospitals|banks|financial institutions|fintech|insurers|insurance|government|mid-market|small businesses|B2B)\b", re.I)),
    ("named_customers", re.compile(r"(?:[Cc]ustomers include|[Tt]rusted by|[Uu]sed by teams at) ([A-Z][^.]+)")),
    ("funding_stage", re.compile(r"\b(Series [A-F]|seed round|seed funding|pre-seed|bootstrapped)\b")),
    ("employee_count", re.compile(r"\b(\d[\d,]*\+?) (employees|people|team members)\b", re.I)),
    ("data_handled", re.compile(r"\b(customer data|personal data|PII|PHI|patient data|health records|payment data|financial data|cardholder data)\b", re.I)),
    ("compliance_claims", COMPLIANCE_RE),
    ("hiring_signals", re.compile(r"\b(we're hiring|we are hiring|now hiring|open role|join us as)\b", re.I)),
    ("business_triggers", re.compile(r"\b(launched|expanding|expansion|opened|new region|announced)\b", re.I)),
    ("existing_compliance_tools", re.compile(r"\b(Vanta|Drata|Secureframe|Thoropass|OneTrust)\b")),
]


def _date(s: str) -> str | None:
    m = DATE_RE.search(s)
    if not m:
        return None
    if m.group(1):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    mon = MONTHS[m.group(4)[:3].lower()]
    day = int(m.group(5) or 1)
    return f"{m.group(6)}-{mon:02d}-{day:02d}"


def _months_between(d: str, today: datetime) -> float:
    dt = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    if today.tzinfo is None:
        today = today.replace(tzinfo=timezone.utc)
    return (today - dt).days / 30.44


def _by_field(ctx) -> dict[str, list[dict]]:
    """Group evidence by the profile fields that cite it (one excerpt may support several fields)."""
    ev = {e["id"]: e for e in ctx["evidence"]}
    out: dict[str, list[dict]] = {}
    for name, f in (ctx.get("profile") or {}).items():
        for eid in f.get("evidence_ids", []):
            if eid in ev:
                out.setdefault(name, []).append(ev[eid])
    return out


class SimulatedLLM:
    label = "SIMULATED LLM (rule-based, offline)"
    simulated = True

    def __init__(self):
        self.calls: list[str] = []

    def structured(self, task, system, context, schema):
        self.calls.append(task)
        return getattr(self, "_" + task)(context)

    # ------------------------------------------------------------ extraction
    def _extract(self, ctx) -> ExtractionOutput:
        found: dict[str, list[tuple[str, str, list[str]]]] = {}
        for page in ctx["pages"]:
            sentences = [s.strip() for s in SENT_SPLIT.split(page["text"]) if s.strip()]
            for i, s in enumerate(sentences):
                if i == 0 and page.get("is_homepage"):
                    m = re.match(r"^([A-Z][A-Za-z0-9]+(?: [A-Z][A-Za-z0-9]+)?) (?:is|helps|builds|provides|makes|bakes)\b", s)
                    if m:
                        found.setdefault("company_name", []).append((page["url"], s, [m.group(1)]))
                for name, rx in RULES:
                    ms = list(rx.finditer(s))
                    if not ms:
                        continue
                    if name == "named_customers":
                        vals = [v.strip() for v in re.split(r",| and ", ms[0].group(1)) if v.strip()]
                    elif name in ("product", "business_model", "hiring_signals", "business_triggers"):
                        vals = [s[:200]]
                    else:
                        vals = [m.group(1) if name == "employee_count" else m.group(0) for m in ms]
                    found.setdefault(name, []).append((page["url"], s, vals))
        fields = []
        for name in ctx["fields"]:
            if name == "domain":
                continue
            hits = found.get(name, [])
            if not hits:
                fields.append(FieldExtraction(name=name, value=None, status="unknown", citations=[]))
                continue
            values: list[str] = []
            for _, _, vs in hits:
                for v in vs:
                    if v not in values:
                        values.append(v)
            cits = [Citation(url=u, excerpt=s, published_at=_date(s)) for u, s, _ in hits[:4]]
            if name in ("company_name", "product", "business_model"):
                value, status = values[0], "observed"
            elif name in ("funding_stage", "employee_count"):
                value = values[0] if len(values) == 1 else values
                status = "observed" if len(values) == 1 else "conflicting"
            else:
                value, status = values, "observed"
            fields.append(FieldExtraction(name=name, value=value, status=status, citations=cits))
        return ExtractionOutput(fields=fields)

    # ------------------------------------------------------------ qualification
    def _qualify(self, ctx) -> QualificationProposal:
        today = datetime.fromisoformat(ctx["today"])
        by_field = _by_field(ctx)
        ids = lambda es: [e["id"] for e in es]  # noqa: E731

        product = by_field.get("product", []) + by_field.get("business_model", [])
        competitor = [e for e in product if re.search(r"compliance automation", e["excerpt"], re.I)]
        unrelated = [e for e in product if re.search(r"\b(bakery|bakes|cafe|restaurant|retail store)\b", e["excerpt"], re.I)
                     and not SOFTWARE_RE.search(e["excerpt"])]
        dims = []
        gaps = []

        # business fit
        cloud = [e for e in by_field.get("business_model", []) + by_field.get("product", [])
                 if re.search(r"\b(SaaS|cloud)\b", e["excerpt"], re.I)]
        software = [e for e in product if SOFTWARE_RE.search(e["excerpt"])]
        data = by_field.get("data_handled", [])
        if cloud and data:
            dims.append(DimensionProposal(dimension="business_fit", level=2, evidence_ids=ids(cloud[:1] + data[:1]), reason="Cloud software handling customer data"))
        elif software:
            dims.append(DimensionProposal(dimension="business_fit", level=1, evidence_ids=ids(software[:1]), reason="Software offering; data handling not stated"))
            gaps.append(EvidenceGap(dimension="business_fit", description="Delivery model / data handled unclear", page_hints=["product", "security", "privacy"]))
        elif unrelated:
            dims.append(DimensionProposal(dimension="business_fit", level=0, evidence_ids=ids(unrelated[:1]), zero_reason="explicit_negative", reason="Business is not software"))
        else:
            dims.append(DimensionProposal(dimension="business_fit", level=0, evidence_ids=[], zero_reason="unknown", reason="No product evidence"))
            gaps.append(EvidenceGap(dimension="business_fit", description="Product unknown", page_hints=["product", "about"]))

        # customer trust
        segs = by_field.get("customer_segments", [])
        strong = [e for e in segs if re.search(r"enterprise|healthcare|hospital|bank|financial|fintech|insur|government", e["excerpt"], re.I)]
        named = by_field.get("named_customers", [])
        if strong or named:
            dims.append(DimensionProposal(dimension="customer_trust", level=2, evidence_ids=ids((strong + named)[:2]), reason="Enterprise/regulated customers or named customers"))
        elif segs:
            dims.append(DimensionProposal(dimension="customer_trust", level=1, evidence_ids=ids(segs[:1]), reason="B2B with limited buyer detail"))
        else:
            dims.append(DimensionProposal(dimension="customer_trust", level=0, evidence_ids=[], zero_reason="unknown", reason="Buyer detail not found"))
            gaps.append(EvidenceGap(dimension="customer_trust", description="Customer segments unknown", page_hints=["customers", "case-studies"]))

        # compliance workload
        comp = by_field.get("compliance_claims", [])
        strong_c = [e for e in comp if re.search(r"Type I|report|certified|audit|attestation|certification", e["excerpt"])]
        if strong_c:
            dims.append(DimensionProposal(dimension="compliance_workload", level=2, evidence_ids=ids(strong_c[:2]), reason="Explicit audit/certification claim"))
        elif comp:
            dims.append(DimensionProposal(dimension="compliance_workload", level=1, evidence_ids=ids(comp[:1]), reason="Broad compliance reference"))
        else:
            dims.append(DimensionProposal(dimension="compliance_workload", level=0, evidence_ids=[], zero_reason="unknown", reason="No compliance statements found (not evidence of absence)"))
            gaps.append(EvidenceGap(dimension="compliance_workload", description="No security/compliance page read yet", page_hints=["security", "trust", "compliance"]))

        # reason now
        hiring = [e for e in by_field.get("hiring_signals", []) if re.search(r"compliance|security|GRC|risk|audit", e["excerpt"], re.I)]
        recent_h = [e for e in hiring if e.get("published_at") and _months_between(e["published_at"], today) <= 6]
        triggers = [e for e in by_field.get("business_triggers", []) if e.get("published_at") and _months_between(e["published_at"], today) <= 12]
        if recent_h:
            dims.append(DimensionProposal(dimension="reason_now", level=2, evidence_ids=ids(recent_h[:1]), reason="Relevant hiring within 6 months"))
        elif triggers:
            dims.append(DimensionProposal(dimension="reason_now", level=1, evidence_ids=ids(triggers[:1]), reason="Dated expansion within 12 months"))
        elif hiring or by_field.get("business_triggers"):
            dims.append(DimensionProposal(dimension="reason_now", level=0, evidence_ids=ids((hiring + by_field.get("business_triggers", []))[:1]), zero_reason="outside_window", reason="Signals undated or too old"))
        else:
            dims.append(DimensionProposal(dimension="reason_now", level=0, evidence_ids=[], zero_reason="unknown", reason="No dated trigger found"))
            gaps.append(EvidenceGap(dimension="reason_now", description="No dated hiring/news found", page_hints=["careers", "jobs", "news", "blog"]))

        # delivery complexity
        frameworks = {k for e in comp for k, rx in FRAMEWORK_KEYS.items() if re.search(rx, e["excerpt"])}
        multi = [e for e in product if re.search(r"integrations|products|environments|regions", e["excerpt"], re.I)]
        if len(frameworks) >= 2:
            fw_ev = [e for e in comp if any(re.search(FRAMEWORK_KEYS[f], e["excerpt"]) for f in frameworks)]
            dims.append(DimensionProposal(dimension="delivery_complexity", level=2, evidence_ids=ids(fw_ev[:2]), reason="Multiple frameworks"))
        elif multi:
            dims.append(DimensionProposal(dimension="delivery_complexity", level=1, evidence_ids=ids(multi[:1]), reason="Integrations or several products"))
        else:
            dims.append(DimensionProposal(dimension="delivery_complexity", level=0, evidence_ids=[], zero_reason="unknown", reason="Not determined"))

        disq, disq_ids = "none", []
        if competitor:
            disq, disq_ids = "compliance_platform_competitor", ids(competitor[:1])
        elif unrelated and not software:
            disq, disq_ids = "unrelated_business", ids(unrelated[:1])
        return QualificationProposal(dimensions=dims, disqualifier=disq, disqualifier_evidence_ids=disq_ids, gaps=gaps)

    def _next_pages(self, ctx) -> NextPagesProposal:
        fetched = set(ctx["fetched"])
        for gap in ctx["gaps"]:
            picks = [c["url"] for c in ctx["candidates"]
                     if c["url"] not in fetched and any(h in c["url"].lower() for h in gap["page_hints"])]
            if picks:
                return NextPagesProposal(gap_dimension=gap["dimension"], gap_description=gap["description"], urls=picks[:2], reason="URL matches gap hints")
        return NextPagesProposal(gap_dimension=None, gap_description="no promising candidates", urls=[], reason="none")

    # ------------------------------------------------------------ pain
    def _pain(self, ctx) -> PainOutput:
        title = ctx["contact"]["title"]
        by_field = _by_field(ctx)
        out = []
        comp = [e for e in by_field.get("compliance_claims", []) if re.search(r"Type I|report|certified|audit", e["excerpt"])]
        if comp:
            out.append(PainProposal(
                observed_fact=comp[0]["excerpt"], evidence_ids=[comp[0]["id"]] + [e["id"] for e in comp[1:2]],
                hypothesis="Keeping audit evidence current between audit periods may take recurring coordination across teams.",
                role_relevance=f"As {title}, this likely sits with you or your team.",
                possible_business_consequence="Evidence gaps found late could make the next audit cycle harder to plan.",
                relevant_capability="continuous_control_monitoring",
                levels=PainLevels(evidence=3, recipient=2, consequence=2, capability=2, honest=1)))
        cust = by_field.get("named_customers", []) + [e for e in by_field.get("customer_segments", []) if re.search(r"enterprise|bank|health|financial|insur", e["excerpt"], re.I)]
        if cust:
            out.append(PainProposal(
                observed_fact=cust[0]["excerpt"], evidence_ids=[e["id"] for e in cust[:2]],
                hypothesis="Selling to enterprise or regulated buyers may bring a steady flow of customer security reviews.",
                role_relevance=f"As {title}, you may be asked to answer or approve those reviews.",
                possible_business_consequence="Slow or inconsistent answers could add friction to customer reviews.",
                relevant_capability="customer_trust_documentation",
                levels=PainLevels(evidence=3 if len(cust) > 1 else 2, recipient=2, consequence=2, capability=2, honest=1)))
        hiring = [e for e in by_field.get("hiring_signals", []) if re.search(r"compliance|security|GRC|audit", e["excerpt"], re.I)]
        if hiring:
            out.append(PainProposal(
                observed_fact=hiring[0]["excerpt"], evidence_ids=[hiring[0]["id"]],
                hypothesis="The open compliance-related role could mean the compliance program is growing in scope.",
                role_relevance=f"As {title}, you may be shaping how that work is organised.",
                possible_business_consequence="A growing program could spread control ownership across more people.",
                relevant_capability="compliance_preparation",
                levels=PainLevels(evidence=2, recipient=2, consequence=1, capability=2, honest=1)))
        return PainOutput(hypotheses=out)

    # ------------------------------------------------------------ email
    def _draft(self, ctx):
        from ..schemas import EmailParts
        h = ctx["primary_hypothesis"]
        fact = ctx["facts"][0]
        hook = fact["excerpt"]
        words = hook.split()
        if len(words) > 28:
            hook = " ".join(words[:28])
        pain = f"{h['hypothesis']} I can't tell from the outside how your team handles this today, so I'd rather ask than assume."
        value = f"That is where Sprinto may be relevant: it offers {ctx['capability']['short_claim']}."
        opening = f"I was reading {ctx['company_name']}'s website and noticed this: \"{hook}\""
        if len(" ".join([opening, pain, value]).split()) < 66:
            pain += " If it is already well covered, that is useful to know too."
        claim = Claim(text=hook, evidence_ids=[fact["evidence_id"]])
        return EmailParts(subject=f"Question about {ctx['company_name']}'s compliance work",
                          greeting=f"Hi {ctx['contact_first_name']},", opening=opening, paragraphs=[pain, value],
                          question="Would a short conversation about how you approach this be useful?",
                          claims=[claim], capability_id=ctx["capability"]["id"])

    def _evaluate_email(self, ctx) -> EmailEvaluation:
        body = ctx["draft"]["body"]
        n = len(body.split())
        issues = []
        hook = 2 if '"' in body else 1
        hyp = 2 if re.search(r"\b(may|might|could)\b", body) else 0
        value = 2 if "Sprinto" in body else 0
        step = 2 if body.count("?") == 1 else 0
        clarity = 2 if 80 <= n <= 120 else 1
        if clarity < 2:
            issues.append(f"length {n} words outside 80-120")
        if step < 2:
            issues.append("needs exactly one question")
        return EmailEvaluation(specific_hook=hook, useful_hypothesis=hyp, relevant_value=value,
                               simple_next_step=step, clarity=clarity, issues=issues)

    # ------------------------------------------------------------ replies
    def _classify_reply(self, ctx) -> ReplyClassification:
        b = ctx["reply_body"]
        low = b.lower()

        def mk(intent, **kw):
            base = dict(slot_reference=None, proposed_time_text=None, timezone_text=None, referral_name=None,
                        referral_title=None, question_summary=None, reason=f"simulated rule: {intent}")
            base.update(kw)
            return ReplyClassification(intent=intent, **base)

        tz = re.search(r"\b(UTC|GMT|IST|PST|PDT|PT|EST|EDT|ET|CET|CEST|BST|CST|SGT|AEST|India time|Asia/Kolkata|[A-Z][a-z]+/[A-Z][A-Za-z_]+)\b", b)
        tz_text = tz.group(1) if tz else None
        if re.search(r"out of (the )?office|on leave|auto-?reply|away until", low):
            return mk("out_of_office")
        if re.search(r"delivery (status notification|failed)|undeliverable|mailer-daemon", low):
            return mk("delivery_notice")
        if re.search(r"unsubscribe|remove me|stop (emailing|contacting)|do not contact|opt out", low):
            return mk("opt_out")
        if re.search(r"not interested|no thanks|not a fit|not relevant", low):
            return mk("reject")
        if re.search(r"pric|cost|discount|quote|contract|sla|guarantee|indemn", low):
            return mk("unsupported_request", question_summary=b[:200])
        m = re.search(r"(?:talk to|reach out to|contact|speak with) (?:my colleague )?([A-Z][a-z]+ [A-Z][a-z]+)(?:,? (?:our|the) ([A-Za-z ]+?))?[.,]", b)
        if m:
            return mk("referral", referral_name=m.group(1), referral_title=(m.group(2) or None))
        opt = re.search(r"\boption ([abc])\b", low)
        if opt and re.search(r"works|confirm|book|good|great|perfect|fine", low):
            return mk("confirm_slot", slot_reference=opt.group(1).upper())
        if re.search(r"\b(confirm|confirmed|book it|that works|sounds good)\b", low) and not re.search(r"\d", low):
            return mk("confirm_slot", slot_reference=None)
        window = re.search(r"\b(next week|following week|this week|week after next|tomorrow|"
                           r"(?:mon|tues|wednes|thurs|fri)day(?: (?:morning|afternoon))?|mornings?|afternoons?)\b", low)
        if re.search(r"how about|instead|can we do|could we do|what about|would .* work|none of those", low) \
                and (re.search(r"\d", low) or window):
            return mk("counterproposal", proposed_time_text=b, timezone_text=tz_text,
                      preferred_window_text=window.group(1) if window else None)
        if re.search(r"send (me )?(more )?(info|information|details)|more about|how does|what does|can you explain", low):
            return mk("info_request", question_summary=b[:200])
        if re.search(r"interested|happy to (chat|talk)|let'?s (talk|chat|meet)|set up a (call|meeting)|open to a (call|chat)", low):
            return mk("interested_meeting")
        return mk("ambiguous")

    # ------------------------------------------------------------ discovery
    def web_research(self, system, prompt, context=None, max_uses=6) -> dict:
        """SIMULATED web search over fixtures/discovery/news.json (fictional articles)."""
        from pathlib import Path
        import json as _json
        self.calls.append("web_research")
        f = context or {}
        path = Path(__file__).resolve().parent.parent.parent / "fixtures" / "discovery" / "news.json"
        arts = _json.loads(path.read_text(encoding="utf-8"))["articles"]
        terms = set()
        for ind in f.get("industries", []):
            terms |= {w.lower() for w in re.findall(r"[A-Za-z]{4,}", ind) if w.lower() not in {"services", "development"}}
        series_words = {"pre_seed": "pre-seed", "seed": "seed", "series_a": "series a", "series_b": "series b",
                        "series_c+": "series c", "pre_series_a": "pre-series a", "pre_series_b": "pre-series b"}
        terms |= {series_words[s] for s in f.get("funding_series", []) if s in series_words}
        if f.get("hiring_roles"):
            terms.add("hiring")
        if f.get("nrr_min_percent"):
            terms.add("retention")
        terms |= {k.lower() for k in f.get("keywords", [])}
        results, cits = [], []
        for a in arts:
            blob = (a["title"] + " " + " ".join(a["sentences"])).lower()
            if terms and not any(t in blob for t in terms):
                continue
            results.append({"url": a["url"], "title": a["title"], "page_age": a["page_age"]})
            for s in a["sentences"]:
                cits.append({"url": a["url"], "title": a["title"], "cited_text": s, "page_age": a["page_age"]})
        return {"text": "simulated research summary", "results": results, "citations": cits, "errors": []}

    def _discover_extract(self, ctx):
        from ..schemas import CompanyEvidenceRef, DiscoveredCompanies, DiscoveredCompany
        name_rx = re.compile(r"^([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?) (?:\(([a-z0-9.-]+\.[a-z]{2,})\) )?"
                             r"(?:raised|raises|reported|reports|closed|is hiring|opened|announced)")
        by_name: dict[str, dict] = {}
        for c in ctx["evidence"]:
            m = name_rx.match(c["cited_text"]) or name_rx.match(c["title"])
            if not m:
                continue
            e = by_name.setdefault(m.group(1), {"domain": None, "refs": [], "text": ""})
            if m.group(2):
                e["domain"] = m.group(2)
            low = c["cited_text"].lower()
            signal = ("nrr" if "retention" in low else "funding" if re.search(r"series|seed", low)
                      else "hiring" if "hiring" in low else "other")
            e["refs"].append(CompanyEvidenceRef(citation_index=c["index"], signal=signal))
            e["text"] += " " + c["cited_text"]
        out = []
        for name, e in by_name.items():
            s = re.search(r"(Series [A-F]|seed round)", e["text"])
            n = re.search(r"\d{2,3}% net revenue retention", e["text"])
            out.append(DiscoveredCompany(name=name, domain=e["domain"], evidence=e["refs"],
                                         funding_series=s.group(1) if s else None,
                                         nrr_text=n.group(0) if n else None))
        return DiscoveredCompanies(companies=out)

    def _answer_info(self, ctx) -> InfoAnswer:
        q = ctx["reply_body"].lower()
        if re.search(r"pric|cost|discount|guarantee|contract|sla|how long|timeline|reference", q):
            return InfoAnswer(capability_ids=[], unanswerable=True, reason="needs non-approved info")
        ids = []
        if re.search(r"evidence|integrat", q):
            ids.append("evidence_collection")
        if re.search(r"monitor", q):
            ids.append("continuous_control_monitoring")
        if re.search(r"trust|questionnaire|customer", q):
            ids.append("customer_trust_documentation")
        if re.search(r"soc|iso|framework|prepar|audit", q) or not ids:
            ids.append("compliance_preparation")
        return InfoAnswer(capability_ids=ids, unanswerable=False, reason="matched approved capabilities")
