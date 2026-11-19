"""Free funding sources that need no API key and no LLM quota.

  news(query)   - Google News RSS and Bing News RSS headlines ("Acme raises $12M Series A").
  form_d(name)  - SEC EDGAR Form D filings: US companies must file one within 15 days of
                  selling shares in a private round. States the amount and date, not the
                  round name (Seed / Series A ...).

Headlines are parsed with fixed rules (parse_headline), never by a model, so a company
only gets a funding round when a headline or a filing states it.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, quote_plus, urlparse

import httpx

UA = "Mozilla/5.0 (compatible; SprintoScraper/1.0; prospecting demo)"

# order matters: the more specific pattern first
HEADLINE_SERIES = [
    ("pre_series_c", r"\bpre[- ]series c\b"), ("series_c+", r"\bseries [c-k](?:\d|\+)?\b"),
    ("pre_series_b", r"\bpre[- ]series b\b"), ("series_b", r"\bseries b(?:\d|\+)?\b"),
    ("pre_series_a", r"\bpre[- ]series a\b"), ("series_a", r"\bseries a(?:\d|\+)?\b"),
    ("pre_seed", r"\bpre[- ]?seed\b"), ("seed", r"\bseed\b"),
]
VERB = re.compile(r"\b(?:has |have )?(raises|raised|raising|secures|secured|lands|landed|bags|bagged|closes|closed|"
                  r"nabs|snags|gets|receives|received|picks up|picked up|completes|scores|announces|grabs|attracts)\b", re.I)
# an amount needs a unit ($14M, €9 million) or at least 6 digits ($250,000): "$20 an hour" is not a round
AMOUNT = re.compile(r"(?:US\$|A\$|C\$|S\$|\$|€|£|₹|Rs\.?\s?|INR\s?|USD\s?|EUR\s?)\s?"
                    r"(?:\d+(?:[.,]\d+)?\s?(?:k|m|mn|million|b|bn|billion|crore|cr|lakh)\b|\d{1,3}(?:,\d{3}){2,}|\d{6,})", re.I)
# raises that are not equity rounds: bonds, prices, pay, forecasts, dividends, public offerings ...
NOT_A_ROUND = re.compile(r"\b(bonds?|notes|debt|loans?|credit (facility|line)|pay|wages?|salar(y|ies)|prices?|pricing|"
                         r"subscriptions?|dividends?|forecasts?|outlook|targets?|guidance|revenue|sales|profits?|"
                         r"earnings|stocks?|shares?|share price|offerings?|preferred|ipo|stakes?|per hour|an hour|"
                         r"/hour|grants?|taxes|tax|rates?|tariffs?|fees?|ceiling|limit|awareness|concerns?|questions?|"
                         r"alarm|eyebrows|bar|stakes)\b", re.I)
FUNDING_WORD = re.compile(r"\b(funding|round|seed|series [a-k]|investment|financing|capital)\b", re.I)
DESCRIPTOR = {"startup", "start-up", "company", "firm", "platform", "maker", "provider", "developer", "based",
              "unicorn", "fintech", "healthtech", "edtech", "insurtech", "proptech", "saas", "biotech", "spinout",
              "spin-off", "exclusive", "report", "breaking", "update", "scaleup", "scale-up", "venture"}
BAD_NAMES = {"it", "this", "the", "startup", "company", "report", "sources", "exclusive", "why", "how", "who", "what"}
# legal-entity endings only: "Reflex Labs, Inc." must not match a different company called "Reflex"
CORP_SUFFIX = re.compile(r"[,.]?\s+(inc|llc|l\.l\.c|corp|corporation|co|ltd|limited|plc|pbc|gmbh|sa|ag|pte|pvt|"
                         r"private)\.?$", re.I)


def series_of(text: str) -> str | None:
    low = (text or "").lower()
    for key, rx in HEADLINE_SERIES:
        if re.search(rx, low):
            return key
    return None


def norm_name(name: str) -> str:
    n = re.sub(r"[’'`]", "", (name or "").strip().lower())
    prev = None
    while prev != n:
        prev, n = n, CORP_SUFFIX.sub("", n).strip()
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def _strip_publisher(title: str) -> str:
    # Google News appends " - Publisher"
    return re.sub(r"\s+[-–|]\s+[^-–|]{2,60}$", "", title.strip())


def _name_from(segment: str) -> str | None:
    seg = segment.split(":")[-1].strip(" ,;\"'“”‘’")
    if "," in seg:
        # "Hammerspace, a data wrangler used by Meta, raises" -> the subject is before the first comma
        seg = seg.split(",")[0].strip()
    words = seg.split()
    run: list[str] = []
    for w in reversed(words):
        core = w.strip(",;\"'“”‘’()")
        if core and (core[0].isupper() or core[0].isdigit()) and core.lower() not in DESCRIPTOR \
                and not core.lower().endswith(("-based", "-founded", "-headquartered", "-born", "-backed")):
            run.insert(0, core)
        else:
            break
        if len(run) == 4:
            break
    name = " ".join(run).strip()
    name = re.split(r"['’]s\s+", name)[-1].strip()  # "Helsinki's Vensum Power" -> "Vensum Power"
    if not name or name.lower() in BAD_NAMES or len(name) < 2:
        return None
    return name


ROUND_CONTEXT = re.compile(r"\b(funding|round|seed|series [a-k]|investors?|led by|venture|vc|backed|backing|"
                           r"financing|investment from)\b", re.I)


def parse_headline(title: str, strict: bool = False) -> dict | None:
    """'Reflex raises $14M Series A to ...' -> {name: Reflex, amount: $14M, series: series_a}. None if not a round.

    strict=True (looking up a known company by name) also needs a round word in the headline, so
    "Meta raises $97.5B for AI compute" is not read as a startup round."""
    t = _strip_publisher(title)
    if NOT_A_ROUND.search(t) or (strict and not ROUND_CONTEXT.search(t)):
        return None
    amount = AMOUNT.search(t)
    series = series_of(t)
    if not (amount or series or FUNDING_WORD.search(t)):
        return None
    name = None
    m = VERB.search(t)
    if m and m.start() > 0:
        name = _name_from(t[:m.start()])
    if not name:
        # "StepSecurity's $20 million Series A"
        p = re.search(r"([A-Z][\w.&-]*(?:\s[A-Z][\w.&-]*){0,3})['’]s\s+(?:US\$|\$|€|£|₹)?\s?[\d.]+", t)
        if p and (series or FUNDING_WORD.search(t[p.end():])):
            name = _name_from(p.group(1))
    if not name or not (amount or series):
        return None
    if not m and not series:
        return None
    return {"name": name, "amount": amount.group(0).strip() if amount else None, "series": series, "headline": t}


class FundingFeeds:
    label = "Google News + Bing News (free, no key)"
    sec_label = "SEC EDGAR Form D (free, US only)"

    def __init__(self, timeout: float = 12):
        self._http = httpx.Client(timeout=timeout, follow_redirects=True, headers={"user-agent": UA})
        self._sec_ua = os.environ.get("SEC_USER_AGENT") or "SprintoScraper/1.0 (prospecting demo)"

    # ---------------- news
    def news(self, query: str, days: int, country: str = "US") -> list[dict]:
        out = self._google(query, days, country) + self._bing(query)
        seen, uniq = set(), []
        for it in out:
            k = _strip_publisher(it["title"]).lower()
            if k not in seen:
                seen.add(k)
                uniq.append(it)
        return uniq

    def _rss(self, url: str, feed: str) -> list[dict]:
        r = self._http.get(url)
        r.raise_for_status()
        items = []
        for it in ET.fromstring(r.content).iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if feed == "Bing News":
                link = (parse_qs(urlparse(link).query).get("url") or [link])[0]
            pub = it.findtext("pubDate")
            try:
                published = parsedate_to_datetime(pub).astimezone(timezone.utc).date().isoformat() if pub else None
            except (TypeError, ValueError):
                published = None
            src = it.find("source")
            items.append({"title": title, "url": link, "published_at": published, "feed": feed,
                          "publisher": (src.text if src is not None else None)})
        return items

    def _google(self, query: str, days: int, country: str) -> list[dict]:
        cc = (country or "US").upper()
        q = quote_plus(f"{query} when:{max(1, days)}d")
        return self._rss(f"https://news.google.com/rss/search?q={q}&hl=en-{cc}&gl={cc}&ceid={cc}:en", "Google News")

    def _bing(self, query: str) -> list[dict]:
        return self._rss(f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&count=50", "Bing News")

    # ---------------- SEC Form D
    def form_d(self, name: str) -> list[dict]:
        target = norm_name(name)
        if not target:
            return []
        # spaces must be %20: the SEC search answers 500 to '+'
        url = f"https://efts.sec.gov/LATEST/search-index?q={quote(chr(34) + name + chr(34), safe='')}&forms=D"
        r = self._http.get(url, headers={"user-agent": self._sec_ua})
        if r.status_code >= 500:
            r = self._http.get(url, headers={"user-agent": self._sec_ua})
        r.raise_for_status()
        hits = []
        for h in (r.json().get("hits") or {}).get("hits") or []:
            s = h.get("_source") or {}
            disp = (s.get("display_names") or [""])[0]
            legal = re.sub(r"\s*\(CIK \d+\)\s*$", "", disp)
            if norm_name(legal) != target:
                continue
            hits.append({"legal_name": legal, "cik": (s.get("ciks") or [""])[0], "adsh": s.get("adsh"),
                         "filed_at": s.get("file_date"), "location": ", ".join(s.get("biz_locations") or [])})
        hits.sort(key=lambda x: x["filed_at"] or "", reverse=True)
        out = []
        for h in hits[:2]:
            cik, nodash = str(int(h["cik"])), (h["adsh"] or "").replace("-", "")
            base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}"
            amount = None
            try:
                x = self._http.get(f"{base}/primary_doc.xml", headers={"user-agent": self._sec_ua})
                if x.status_code == 200:
                    doc = ET.fromstring(x.content)
                    sold = doc.findtext(".//totalAmountSold")
                    if sold and sold.replace(".", "").isdigit() and float(sold) > 0:
                        amount = f"${float(sold):,.0f}"
            except (httpx.HTTPError, ET.ParseError):
                pass
            out.append({**h, "amount": amount, "url": f"{base}/{h['adsh']}-index.htm"})
        return out


class SimFundingFeeds:
    """Simulation mode makes no network calls: the free feeds return nothing."""
    label = "Funding news (not used in simulation)"
    sec_label = "SEC Form D (not used in simulation)"

    def news(self, query, days, country="US"):
        return []

    def form_d(self, name):
        return []
