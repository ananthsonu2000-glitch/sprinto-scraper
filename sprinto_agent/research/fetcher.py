"""Website reader: guarded HTTP fetcher with optional browser fallback, plus a
fixture fetcher for SIMULATION mode.

Retrieval failures (403, timeouts, robots disallow, unsafe redirects) are recorded
as failures — they are never treated as negative evidence about the company.
"""
from __future__ import annotations

import json
import re
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .. import clock
from ..errors import ErrorKind, IntegrationError
from .url_guard import UnsafeURLError, check_url, same_site

USER_AGENT = "SprintoDemoResearchBot/0.1 (+demo; respects robots.txt)"


@dataclass
class FetchResult:
    url: str
    final_url: str | None
    status: str  # ok | http_error | blocked | unsafe | robots_disallowed | not_html | too_large | failed
    http_status: int | None = None
    title: str | None = None
    text: str = ""
    links: list[dict] = field(default_factory=list)
    error: str | None = None
    method: str = "http"
    retrieved_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def html_to_parts(html: str, base_url: str) -> tuple[str | None, str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if href.startswith(("http://", "https://")):
            links.append({"url": href, "text": a.get_text(" ", strip=True)[:80]})
    for tag in soup(["head", "title", "script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    # Menus, headers and footers are boilerplate, not company evidence; drop them so a page
    # that is "all menu" is recognised as thin (and can trigger the browser fallback).
    for tag in soup(["nav", "header", "footer"]):
        tag.decompose()
    for tag in soup.select('[role="navigation"], [role="banner"], [role="contentinfo"], [aria-hidden="true"]'):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    # de-dup links
    seen, uniq = set(), []
    for l in links:
        if l["url"] not in seen:
            seen.add(l["url"])
            uniq.append(l)
    return title, text, uniq


class HttpFetcher:
    label = "live website (HTTP)"

    def __init__(self, settings, policy: dict, resolver=None, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.max_redirects = policy["research"]["max_redirects"]
        self.min_chars = policy["research"]["min_text_chars_before_browser_fallback"]
        self._resolver_kwargs = {"resolver": resolver} if resolver else {}
        self._client = httpx.Client(
            follow_redirects=False, timeout=settings.fetch_timeout_seconds, transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _check(self, url):
        return check_url(url, **self._resolver_kwargs)

    def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = None
            try:
                robots_url = self._check(origin + "/robots.txt")
                r = self._client.get(robots_url)
                if r.status_code == 200 and len(r.content) < 500_000:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except (httpx.HTTPError, UnsafeURLError):
                rp = None
            self._robots[origin] = rp
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> FetchResult:
        now = clock.iso(clock.now())
        try:
            current = self._check(url)
        except UnsafeURLError as e:
            return FetchResult(url, None, "unsafe", error=str(e), retrieved_at=now)
        start_host = urlsplit(current).hostname
        if not self._robots_allows(current):
            return FetchResult(url, current, "robots_disallowed", error="robots.txt disallows", retrieved_at=now)
        for _hop in range(self.max_redirects + 1):
            try:
                with self._client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            return FetchResult(url, current, "failed", resp.status_code, error="redirect without location", retrieved_at=now)
                        nxt = urljoin(current, loc)
                        try:
                            nxt = self._check(nxt)  # validate EVERY redirect destination
                        except UnsafeURLError as e:
                            return FetchResult(url, current, "unsafe", resp.status_code, error=f"unsafe redirect: {e}", retrieved_at=now)
                        if not same_site(nxt, (start_host or "").removeprefix("www.")):
                            return FetchResult(url, current, "unsafe", resp.status_code,
                                               error=f"cross-site redirect not followed: {nxt}", retrieved_at=now)
                        current = nxt
                        continue
                    if resp.status_code in (401, 403, 429) or resp.status_code >= 400:
                        kind = "blocked" if resp.status_code in (401, 403, 429) else "http_error"
                        if resp.status_code >= 500:
                            raise IntegrationError(ErrorKind.TRANSIENT, f"HTTP {resp.status_code}", "website")
                        return FetchResult(url, current, kind, resp.status_code, error=f"HTTP {resp.status_code}", retrieved_at=now)
                    ctype = resp.headers.get("content-type", "")
                    if "html" not in ctype and "text/plain" not in ctype:
                        return FetchResult(url, current, "not_html", resp.status_code, error=f"content-type {ctype}", retrieved_at=now)
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.fetch_max_bytes:
                            return FetchResult(url, current, "too_large", resp.status_code, error="response too large", retrieved_at=now)
                    html = body.decode(resp.encoding or "utf-8", errors="replace")
                    title, text, links = html_to_parts(html, current)
                    result = FetchResult(url, current, "ok", resp.status_code, title, text, links, retrieved_at=now)
                    if len(text) < self.min_chars and self.settings.browser_fallback:
                        rendered = self._browser_fetch(current)
                        if rendered is not None and len(rendered.text) > len(text):
                            return rendered
                    return result
            except httpx.TimeoutException as e:
                raise IntegrationError(ErrorKind.TRANSIENT, f"timeout: {e}", "website")
            except httpx.TransportError as e:
                raise IntegrationError(ErrorKind.TRANSIENT, f"transport error: {e}", "website")
        return FetchResult(url, current, "failed", error="too many redirects", retrieved_at=now)

    def _browser_fetch(self, url: str) -> FetchResult | None:
        """Optional Playwright render. Every request the page makes is re-checked by the URL guard."""
        try:
            from playwright.sync_api import sync_playwright  # optional dependency
        except ImportError:
            return None
        now = clock.iso(clock.now())
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(user_agent=USER_AGENT)

                checked: dict[str, bool] = {}

                def guard(route):
                    req = route.request
                    try:
                        if req.resource_type in ("image", "media", "font"):
                            route.abort()  # not needed for text; saves time and bandwidth
                            return
                        host = urlsplit(req.url).hostname or ""
                        if req.url.startswith(("data:", "blob:")):
                            route.continue_()
                            return
                        if host not in checked:  # DNS/SSRF check once per host
                            try:
                                self._check(req.url)
                                checked[host] = True
                            except UnsafeURLError:
                                checked[host] = False
                        route.continue_() if checked[host] else route.abort()
                    except Exception:  # noqa: BLE001 - page/browser may already be closing
                        pass

                page.route("**/*", guard)
                page.goto(url, timeout=int(self.settings.fetch_timeout_seconds * 1000) * 2, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)  # let client-side rendering finish
                html = page.content()
                final = page.url
                page.unroute_all(behavior="ignoreErrors")
                browser.close()
            self._check(final)
            title, text, links = html_to_parts(html[: self.settings.fetch_max_bytes], final)
            return FetchResult(url, final, "ok", 200, title, text, links, method="browser", retrieved_at=now)
        except Exception as e:  # noqa: BLE001 - fallback must never crash the run
            return FetchResult(url, url, "failed", error=f"browser fallback failed: {e}", method="browser", retrieved_at=now)


class FixtureFetcher:
    """SIMULATION ONLY. Serves fixtures/sites/<domain>.json:
    {"pages": {"/path": "<html>", ...}, "status": {"/path": 403 | "timeout" | {"redirect": url}}}"""

    label = "SIMULATED website fixtures"

    def __init__(self, fixtures_dir: str):
        self.root = Path(fixtures_dir) / "sites"
        self.calls: list[str] = []

    def _site(self, host: str) -> dict:
        f = self.root / f"{host}.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"pages": {}, "status": {}}

    def fetch(self, url: str) -> FetchResult:
        now = clock.iso(clock.now())
        self.calls.append(url)
        try:
            url = check_url(url, resolve=False)
        except UnsafeURLError as e:
            return FetchResult(url, None, "unsafe", error=str(e), method="fixture", retrieved_at=now)
        parts = urlsplit(url)
        host = (parts.hostname or "").removeprefix("www.")
        site = self._site(host)
        path = parts.path.rstrip("/") or "/"
        st = site.get("status", {}).get(path)
        if st == "timeout":
            raise IntegrationError(ErrorKind.TRANSIENT, "simulated timeout", "website")
        if isinstance(st, dict) and "redirect" in st:
            try:
                check_url(st["redirect"], resolve=False)
            except UnsafeURLError as e:
                return FetchResult(url, url, "unsafe", 302, error=f"unsafe redirect: {e}", method="fixture", retrieved_at=now)
            return self.fetch(st["redirect"])
        if isinstance(st, int):
            kind = "blocked" if st in (401, 403, 429) else "http_error"
            return FetchResult(url, url, kind, st, error=f"HTTP {st}", method="fixture", retrieved_at=now)
        html = site.get("pages", {}).get(path)
        if html is None:
            return FetchResult(url, url, "http_error", 404, error="HTTP 404", method="fixture", retrieved_at=now)
        title, text, links = html_to_parts(html, url)
        return FetchResult(url, url, "ok", 200, title, text, links, method="fixture", retrieved_at=now)
