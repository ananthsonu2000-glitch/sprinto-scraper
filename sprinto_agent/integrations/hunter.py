"""Hunter.io — the ONLY contact-data provider. Domain Search endpoint.

Prospect emails returned here are research data only. They are never passed to
any delivery integration (see delivery_guard.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from ..errors import ErrorKind, IntegrationError

API = "https://api.hunter.io/v2"


def _classify(resp: httpx.Response) -> IntegrationError:
    try:
        errs = resp.json().get("errors", [])
    except ValueError:
        errs = []
    detail = "; ".join(f"{e.get('id', '')}: {e.get('details', '')}" for e in errs) or resp.text[:200]
    code = resp.status_code
    low = detail.lower()
    if code == 401:
        return IntegrationError(ErrorKind.AUTH, f"Hunter rejected the API key ({detail})", "hunter")
    if code in (402, 403) or ("limit" in low and ("usage" in low or "plan" in low or "quota" in low)):
        return IntegrationError(ErrorKind.QUOTA, f"Hunter quota/plan limit: {detail}", "hunter")
    if code == 429:
        if "usage" in low or "quota" in low or "credits" in low:
            return IntegrationError(ErrorKind.QUOTA, f"Hunter quota exhausted: {detail}", "hunter")
        return IntegrationError(ErrorKind.TRANSIENT, f"Hunter rate limit: {detail}", "hunter")
    if code in (400, 422):
        return IntegrationError(ErrorKind.INVALID_INPUT, f"Hunter rejected input: {detail}", "hunter")
    if code >= 500:
        return IntegrationError(ErrorKind.TRANSIENT, f"Hunter server error {code}", "hunter")
    return IntegrationError(ErrorKind.PERMANENT, f"Hunter error {code}: {detail}", "hunter")


class HunterClient:
    label = "Hunter.io (live)"
    simulated = False

    def __init__(self, api_key: str, limit: int = 10, transport: httpx.BaseTransport | None = None):
        self._key = api_key
        self.limit = limit
        self._http = httpx.Client(timeout=15, transport=transport)

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self._http.get(f"{API}/{path}", params={**params, "api_key": self._key})
        except httpx.TimeoutException as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"Hunter timeout: {e}", "hunter")
        except httpx.TransportError as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"Hunter connection error: {e}", "hunter")
        if r.status_code != 200:
            raise _classify(r)
        return r.json()

    def domain_search(self, domain: str) -> dict:
        """Returns Hunter's `data` object (organization info + `emails` list)."""
        return self._get("domain-search", {"domain": domain, "limit": self.limit}).get("data", {})

    def account(self) -> dict:
        """Account/quota information, shown on the readiness panel (no assumed allowances)."""
        return self._get("account", {}).get("data", {})

    def discover(self, body: dict) -> dict:
        """Hunter Discover (company search by filters). Returns {"data": [...], "meta": {...}}.
        Hunter silently ignores filters the plan does not include; meta.filters lists what was applied."""
        try:
            r = self._http.post(f"{API}/discover", params={"api_key": self._key}, json=body)
        except httpx.TimeoutException as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"Hunter timeout: {e}", "hunter")
        except httpx.TransportError as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"Hunter connection error: {e}", "hunter")
        if r.status_code == 403 and "no_discover_access" in r.text:
            raise IntegrationError(ErrorKind.AUTH, "Your Hunter plan does not include Discover (no_discover_access)", "hunter")
        if r.status_code != 200:
            raise _classify(r)
        return r.json()


class SimulatedHunter:
    """SIMULATION ONLY: fixtures/hunter/<domain>.json (Hunter-shaped `data`)."""

    label = "SIMULATED Hunter (fixtures)"
    simulated = True

    def __init__(self, fixtures_dir: str):
        self.root = Path(fixtures_dir) / "hunter"
        self.calls: list[str] = []
        self.force_error: str | None = None  # tests: "quota" | "auth" | "transient"

    def domain_search(self, domain: str) -> dict:
        self.calls.append(domain)
        if self.force_error:
            kind = {"quota": ErrorKind.QUOTA, "auth": ErrorKind.AUTH, "transient": ErrorKind.TRANSIENT}[self.force_error]
            raise IntegrationError(kind, f"simulated {self.force_error} error", "hunter")
        f = self.root / f"{domain}.json"
        if not f.exists():
            return {"domain": domain, "emails": []}
        return json.loads(f.read_text(encoding="utf-8"))

    def account(self) -> dict:
        return {"plan_name": "SIMULATED", "requests": {"searches": {"used": len(self.calls), "available": "simulated"}}}

    def discover(self, body: dict) -> dict:
        """SIMULATED Discover over fixtures/discovery/hunter_discover.json. Mimics a plan
        without paid filters: `funding` (and other paid-only filters) are ignored, like Hunter does."""
        self.calls.append("discover")
        if self.force_error:
            kind = {"quota": ErrorKind.QUOTA, "auth": ErrorKind.AUTH, "transient": ErrorKind.TRANSIENT}[self.force_error]
            raise IntegrationError(kind, f"simulated {self.force_error} error", "hunter")
        fx = json.loads((self.root.parent / "discovery" / "hunter_discover.json").read_text(encoding="utf-8"))
        paid = fx.get("plan_has_paid_filters", False)
        applied = {}
        rows = fx["companies"]
        if "organization" in body:
            names = {n.lower() for n in body["organization"].get("name", [])}
            doms = set(body["organization"].get("domain", []))
            rows = [c for c in rows if c["organization"].lower() in names or c["domain"] in doms]
            applied["organization"] = body["organization"]
        if "industry" in body:
            inc = set(body["industry"].get("include", []))
            rows = [c for c in rows if c["industry"] in inc]
            applied["industry"] = body["industry"]
        if "headcount" in body:
            rows = [c for c in rows if c["headcount"] in body["headcount"]]
            applied["headcount"] = body["headcount"]
        if "headquarters_location" in body:
            countries = {x.get("country") for x in body["headquarters_location"].get("include", [])}
            rows = [c for c in rows if c["country"] in countries]
            applied["headquarters_location"] = body["headquarters_location"]
        if "job_openings" in body:
            words = [w.lower() for w in body["job_openings"].get("job_title", {}).get("include", [])]
            rows = [c for c in rows if any(w in j.lower() for j in c.get("open_roles", []) for w in words)]
            applied["job_openings"] = body["job_openings"]
        if "keywords" in body:
            words = [w.lower() for w in body["keywords"].get("include", [])]
            rows = [c for c in rows if any(w in " ".join(c.get("keywords", [])).lower() for w in words)]
            applied["keywords"] = body["keywords"]
        if "funding" in body and paid:
            rows = [c for c in rows if c.get("funding_series") in body["funding"].get("series", [])]
            applied["funding"] = body["funding"]
        rows = rows[: body.get("limit", 100)]
        return {"data": [{"domain": c["domain"], "organization": c["organization"],
                          "emails_count": {"personal": 3, "generic": 1, "total": 4}} for c in rows],
                "meta": {"results": len(rows), "limit": body.get("limit", 100), "offset": 0, "params": body,
                         "filters": applied}}
