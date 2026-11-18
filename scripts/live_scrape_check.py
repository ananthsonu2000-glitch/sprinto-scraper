"""Live check of the website reader on real domains (no API keys needed, nothing stored).

  python scripts/live_scrape_check.py chargebee.com postman.com

Does exactly what the pipeline's initial research does: homepage, then up to 3 relevant
pages picked from its links, all through the SSRF guard and robots.txt. Prints what was
read and any compliance wording found (a preview of the evidence the LLM would cite).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sprinto_agent.config import Mode, Settings, load_versions  # noqa: E402
from sprinto_agent.errors import IntegrationError  # noqa: E402
from sprinto_agent.research.agent import select_initial_pages  # noqa: E402
from sprinto_agent.research.fetcher import HttpFetcher  # noqa: E402
from sprinto_agent.research.url_guard import normalize_domain  # noqa: E402

COMPLIANCE = re.compile(r"[^.]{0,120}\b(SOC ?2[^.]{0,40}|ISO(?:/IEC)? ?27001[^.]{0,30}|HIPAA|GDPR|PCI[- ]DSS)[^.]{0,80}\.", re.I)


def main(domains):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    s = Settings.from_env(mode=Mode.DRY_RUN)  # honours BROWSER_FALLBACK from .env
    pol = load_versions().policy
    f = HttpFetcher(s, pol)
    for raw in domains:
        d = normalize_domain(raw)
        print(f"\n=== {raw} -> {d}")
        if not d:
            print("  rejected by domain normalisation")
            continue
        pages = []

        def fetch(url, why):
            try:
                r = f.fetch(url)
            except IntegrationError as e:
                print(f"  [failed] {url}  {e.message}")
                return None
            print(f"  [{r.status:<17}] {url}  ({len(r.text)} chars, via {r.method}) {why}" + (f"  {r.error}" if r.error else ""))
            pages.append({"url": url, "final_url": r.final_url, "status": r.status, "links": r.links, "text": r.text})
            return r

        home = fetch(f"https://{d}/", "homepage")
        if home is None or not home.ok:
            home = fetch(f"https://www.{d}/", "homepage (www)")
        if home is None or not home.ok:
            continue
        for u, why in select_initial_pages(d, pages, {"research": pol["research"]}):
            fetch(u, why)
        found = []
        for p in pages:
            for m in COMPLIANCE.finditer(p["text"] or ""):
                sent = " ".join(m.group(0).split())
                if sent not in found:
                    found.append(sent)
        print(f"  compliance wording found ({len(found)}):")
        for x in found[:4]:
            print("   -", x[:220])


if __name__ == "__main__":
    main(sys.argv[1:] or ["chargebee.com", "postman.com", "freshworks.com"])
