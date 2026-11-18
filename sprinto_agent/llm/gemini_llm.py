"""Google Gemini adapter (REST) with a ranked model chain and automatic fallback.

* The best available model is tried first; on ANY failure (model unavailable, quota or
  rate limit, server error/overload, timeout, blocked or truncated output, or output that
  fails schema validation) the next-best model is tried, down the chain.
* Models that just failed are put in a short cooldown so later calls try healthier ones first.
* Authentication failures (bad/revoked key) stop immediately — every model would fail.
* structured(): JSON output constrained by the pydantic schema, then validated in code.
* web_research(): Google Search grounding. Citations are the model's grounded statements
  plus the source links Google returned (not verbatim page quotes) — labelled as such.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from ..errors import ErrorKind, IntegrationError
from .base import T, render_user_prompt

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta"

# Never auto-selected: non-text or special-purpose variants, and unnamed experimental models.
EXCLUDE = re.compile(r"(tts|image|transcribe|computer-use|customtools|embedding|live|audio|native|exp|learnlm|gemma)", re.I)
NAME_RE = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-(pro|flash-lite|flash)(?:-(preview))?(?:-[\w.-]+)?$")
TIER = {"pro": 3, "flash": 2, "flash-lite": 1}
DEFAULT_CHAIN = ["gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]


def rank_models(names: list[str]) -> list[str]:
    """Best first: newest major generation; then pro > flash > flash-lite; then newer minor; stable before preview."""
    scored = []
    for full in names:
        n = full.removeprefix("models/")
        if EXCLUDE.search(n):
            continue
        m = NAME_RE.match(n)
        if not m:
            continue  # unknown naming (e.g. codenames) -> only via explicit GEMINI_MODELS
        major, minor, tier, preview = int(m.group(1)), int(m.group(2) or 0), m.group(3), m.group(4)
        if n.count("-") > (3 if preview else 2) + (1 if tier == "flash-lite" else 0):
            continue  # dated/odd variants like -preview-09-2025
        scored.append(((major, TIER[tier], minor, 0 if preview else 1), n))
    scored.sort(reverse=True)
    return [n for _, n in scored]


def inline_refs(schema: dict) -> dict:
    """Gemini accepts a JSON-schema subset; inline $defs/$ref and drop unsupported keys."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title", "default")}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)


PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini daily quotas reset at midnight Pacific time


def next_quota_reset(now: float | None = None) -> float:
    n = datetime.fromtimestamp(now or time.time(), tz=PACIFIC)
    return (n.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def fmt_reset(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Kolkata")).strftime("%d %b %H:%M IST")


class QuotaState:
    """Shared across processes (worker, dashboard, scripts) via a small JSON file, so every
    process respects the same quota holds instead of re-hitting the limits."""

    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.data = {"hold_until": {}, "no_free_quota": [], "retired": [], "search_hold_until": 0}
        self.load()

    def load(self):
        if self.path and self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def hold(self, model: str, until: float):
        self.load()
        self.data["hold_until"][model] = max(until, self.data["hold_until"].get(model, 0))
        self.save()

    def no_free(self, model: str):
        self.load()
        if model not in self.data["no_free_quota"]:
            self.data["no_free_quota"].append(model)
        self.save()

    def retire(self, model: str):
        self.load()
        self.data.setdefault("retired", [])
        if model not in self.data["retired"]:
            self.data["retired"].append(model)
        self.save()

    def held(self, model: str, now: float) -> bool:
        return (self.data["hold_until"].get(model, 0) > now or model in self.data["no_free_quota"]
                or model in self.data.get("retired", []))

    def hold_search(self, until: float):
        self.load()
        self.data["search_hold_until"] = max(until, self.data.get("search_hold_until", 0))
        self.save()


def _quota_info(r: httpx.Response) -> dict:
    """Read Google's 429 details: retry delay, per-day vs per-minute, and 'limit: 0' (no free quota)."""
    try:
        err = r.json().get("error", {})
    except ValueError:
        return {"retry": None, "per_day": False, "zero": False, "has_details": False, "message": r.text[:200]}
    msg = err.get("message", "")
    retry, per_day, has = None, False, False
    for d in err.get("details", []) or []:
        t = d.get("@type", "")
        if t.endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", d.get("retryDelay", ""))
            retry = float(m.group(1)) if m else None
        if t.endswith("QuotaFailure"):
            has = True
            per_day = per_day or any("PerDay" in (v.get("quotaId") or "") for v in d.get("violations", []))
    return {"retry": retry, "per_day": per_day, "zero": bool(re.search(r"limit:\s*0\b", msg)),
            "has_details": has, "message": msg}


class GeminiLLM:
    simulated = False

    def __init__(self, api_key: str, models: list[str] | None = None, search_models: list[str] | None = None,
                 timeout: float = 90.0, max_models: int = 10, cooldown_seconds: int = 300,
                 transport: httpx.BaseTransport | None = None, on_event: Callable[[dict], None] | None = None,
                 free_tier: bool = False, state_path: str | None = None):
        self._key = api_key
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self.max_models = max_models
        self.cooldown_seconds = cooldown_seconds
        self.on_event = on_event
        self.free_tier = free_tier
        self.quota = QuotaState(state_path)
        self._cooldown: dict[str, float] = {}   # soft: overload/timeouts -> tried last
        self._disabled: set[str] = set()       # retired/unknown models (404)
        self.last_model: str | None = None
        ranked = models or self._discover()
        if free_tier and not models:
            # Pro models have no free quota ("limit: 0"); never spend calls on them in free-tier mode.
            ranked = [m for m in ranked if not (NAME_RE.match(m) and NAME_RE.match(m).group(3) == "pro")]
        self.chain = ranked[:max_models]
        self.search_chain = (search_models or self.chain)[:max_models]
        tier = "free tier, " if free_tier else ""
        self.label = f"Google Gemini ({tier}{self.chain[0] if self.chain else 'no model'} + {max(0, len(self.chain) - 1)} fallbacks)"

    def quota_status(self) -> dict:
        self.quota.load()
        now = time.time()
        held = {m: fmt_reset(t) for m, t in self.quota.data["hold_until"].items() if t > now and m in self.chain}
        s = self.quota.data.get("search_hold_until", 0)
        return {"free_tier": self.free_tier, "on_quota_hold": held,
                "no_free_quota": self.quota.data["no_free_quota"],
                "available_now": [m for m in self.chain if not self.quota.held(m, now) and m not in self._disabled],
                "search": f"free Google Search allowance used up until {fmt_reset(s)}" if s > now else "available"}

    # ------------------------------------------------------------------ model list
    def _discover(self) -> list[str]:
        try:
            r = self._http.get(f"{API}/models", headers={"x-goog-api-key": self._key}, params={"pageSize": 200})
        except httpx.HTTPError:
            return list(DEFAULT_CHAIN)
        if r.status_code in (400, 401, 403):
            raise IntegrationError(ErrorKind.AUTH, f"Gemini rejected the API key (HTTP {r.status_code})", "llm")
        if r.status_code != 200:
            return list(DEFAULT_CHAIN)
        names = [m["name"] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        return rank_models(names) or list(DEFAULT_CHAIN)

    def _ordered(self, chain: list[str]) -> list[str]:
        now = time.time()
        self.quota.load()
        # models on a quota hold are skipped entirely (no wasted call); overloaded ones are tried last
        live = [m for m in chain if m not in self._disabled and not self.quota.held(m, now)]
        healthy = [m for m in live if self._cooldown.get(m, 0) <= now]
        cooling = [m for m in live if self._cooldown.get(m, 0) > now]
        return healthy + cooling

    def _emit(self, **e):
        if self.on_event:
            try:
                self.on_event(e)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ core call with fallback
    def _run(self, task: str, chain: list[str], body: dict, accept: Callable[[dict], object]):
        failures = []
        search = "tools" in body
        quota_failures = 0
        order = self._ordered(chain)
        if not order:
            holds = [self.quota.data["hold_until"].get(m, 0) for m in chain if m not in self.quota.data["no_free_quota"]]
            reset = min([h for h in holds if h > time.time()], default=next_quota_reset())
            raise IntegrationError(ErrorKind.QUOTA, f"free Gemini quota used up on every model until {fmt_reset(reset)}", "llm")
        for model in order:
            t0 = time.time()
            req = body
            m = NAME_RE.match(model)
            if "generationConfig" in body and "tools" not in body and m and int(m.group(1)) >= 3:
                # Gemini 3+: low thinking effort for schema-bound tasks keeps calls within the timeout
                req = {**body, "generationConfig": {**body["generationConfig"], "thinkingConfig": {"thinkingLevel": "low"}}}
            try:
                r = self._http.post(f"{API}/models/{model}:generateContent",
                                    headers={"x-goog-api-key": self._key}, json=req)
            except httpx.TimeoutException:
                failures.append((model, "timeout"))
                self._cooldown[model] = time.time() + self.cooldown_seconds
                continue
            except httpx.TransportError as e:
                failures.append((model, f"connection error: {e}"))
                continue
            if r.status_code != 200:
                msg = _err_message(r)
                if r.status_code in (401,) or (r.status_code in (400, 403) and "api key" in msg.lower()):
                    raise IntegrationError(ErrorKind.AUTH, f"Gemini rejected the API key: {msg}", "llm")
                if r.status_code == 404:
                    self._disabled.add(model)
                    self.quota.retire(model)  # retired / unknown: skip in every process from now on
                elif r.status_code == 429 and "quota" in msg.lower():
                    q = _quota_info(r)
                    quota_failures += 1
                    if q["zero"]:
                        self.quota.no_free(model)  # no free quota at all for this model
                        why = "no free quota for this model"
                    elif search and not q["has_details"]:
                        # Google Search grounding allowance (not tied to one model)
                        why = "free Google Search allowance used up"
                    else:
                        until = time.time() + q["retry"] if q["retry"] else (
                            next_quota_reset() if q["per_day"] else time.time() + 3600)
                        self.quota.hold(model, min(until, time.time() + 26 * 3600))
                        why = f"free quota used up until {fmt_reset(min(until, time.time() + 26 * 3600))}"
                    failures.append((model, f"HTTP 429: {why}"))
                    if search and not q["has_details"] and quota_failures >= 2:
                        # two models in a row refused search for quota -> stop burning the chain
                        self.quota.hold_search(next_quota_reset())
                        break
                    continue
                elif r.status_code in (429, 500, 502, 503, 504):
                    self._cooldown[model] = time.time() + self.cooldown_seconds
                failures.append((model, f"HTTP {r.status_code}: {msg[:120]}"))
                continue
            data = r.json()
            try:
                result = accept(data)
            except _Reject as e:
                failures.append((model, str(e)))
                continue
            self.last_model = model
            self._emit(task=task, model=model, ok=True, seconds=round(time.time() - t0, 1),
                       fell_back_from=[f for f, _ in failures])
            if failures:
                log.warning("gemini %s served by %s after: %s", task, model, failures)
            return result
        self._emit(task=task, model=None, ok=False, failures=failures)
        detail = "; ".join(f"{m}: {e}" for m, e in failures) or "no models available"
        if search and self.quota.data.get("search_hold_until", 0) > time.time():
            raise IntegrationError(ErrorKind.QUOTA, "free Google Search allowance used up until "
                                   f"{fmt_reset(self.quota.data['search_hold_until'])}", "llm")
        if failures and quota_failures == len(failures):
            raise IntegrationError(ErrorKind.QUOTA, f"free Gemini quota used up for {task}: {detail}", "llm")
        raise IntegrationError(ErrorKind.TRANSIENT, f"all Gemini models failed for {task}: {detail}", "llm")

    # ------------------------------------------------------------------ public API
    def structured(self, task: str, system: str, context: dict, schema: type[T]) -> T:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": render_user_prompt(context)}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseJsonSchema": inline_refs(schema.model_json_schema()),
                                 "maxOutputTokens": 16384, "temperature": 0.2},
        }

        def accept(data):
            text = _text(data)
            try:
                return schema.model_validate_json(_strip_fence(text))
            except ValidationError as e:
                raise _Reject(f"output failed schema validation: {str(e)[:150]}")
        return self._run(task, self.chain, body, accept)

    def web_research(self, system: str, prompt: str, context: dict | None = None, max_uses: int = 6) -> dict:
        self.quota.load()
        hold = self.quota.data.get("search_hold_until", 0)
        if hold > time.time():  # don't spend calls while the free search allowance is used up
            raise IntegrationError(ErrorKind.QUOTA, f"free Google Search allowance used up until {fmt_reset(hold)}", "llm")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"maxOutputTokens": 16384, "temperature": 0.2},
        }

        def accept(data):
            text = _text(data)
            gm = (data.get("candidates") or [{}])[0].get("groundingMetadata") or {}
            chunks = gm.get("groundingChunks") or []
            supports = gm.get("groundingSupports") or []
            if not chunks:
                raise _Reject("no grounded search results returned")
            results = [{"url": (c.get("web") or {}).get("uri", ""), "title": (c.get("web") or {}).get("title", ""),
                        "page_age": None} for c in chunks]
            citations = []
            for s in supports:
                seg = (s.get("segment") or {}).get("text", "")
                for i in s.get("groundingChunkIndices") or []:
                    if 0 <= i < len(results) and seg:
                        citations.append({"url": results[i]["url"], "title": results[i]["title"], "cited_text": seg,
                                          "page_age": None, "kind": "grounded statement (Gemini + Google Search)"})
            return {"text": text, "results": results, "citations": citations, "errors": [],
                    "queries": gm.get("webSearchQueries") or []}
        return self._run("web_research", self.search_chain, body, accept)


class _Reject(Exception):
    """A 200 response that is unusable (blocked, truncated, empty, invalid) -> try next model."""


def _text(data: dict) -> str:
    fb = data.get("promptFeedback") or {}
    if fb.get("blockReason"):
        raise _Reject(f"prompt blocked: {fb['blockReason']}")
    cands = data.get("candidates") or []
    if not cands:
        raise _Reject("no candidates returned")
    c = cands[0]
    reason = c.get("finishReason", "STOP")
    text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
    if reason not in ("STOP", "FINISH_REASON_UNSPECIFIED", None) or not text.strip():
        raise _Reject(f"unusable output (finishReason={reason}, {len(text)} chars)")
    return text


def _strip_fence(t: str) -> str:
    t = t.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    return t


def _err_message(r: httpx.Response) -> str:
    try:
        return r.json().get("error", {}).get("message", r.text[:200])
    except (ValueError, json.JSONDecodeError):
        return r.text[:200]
