"""Live setup check — run after filling in .env.

  python scripts/check_setup.py

Makes only free / read-only calls. Sends nothing, books nothing, prints no secrets:
  * website reader : fetches https://example.com through the SSRF guard
  * Anthropic      : models.retrieve(LLM_MODEL)  (verifies the key and model access)
  * Hunter         : /v2/account (plan + remaining quota) and a 1-result Discover call
  * Google         : refresh token -> Gmail profile (sender address) -> Calendar free/busy (next hour)
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sprinto_agent import clock  # noqa: E402
from sprinto_agent.config import Settings, load_versions  # noqa: E402
from sprinto_agent.errors import IntegrationError  # noqa: E402

OK, FAIL, SKIP = "OK  ", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name, fn):
    try:
        detail = fn()
        results.append((OK, name, detail or ""))
    except Skip as e:
        results.append((SKIP, name, str(e)))
    except IntegrationError as e:
        results.append((FAIL, name, f"{e.kind}: {e.message}"))
    except Exception as e:  # noqa: BLE001
        results.append((FAIL, name, f"{type(e).__name__}: {e}"))


class Skip(Exception):
    pass


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    s = Settings.from_env()
    v = load_versions()
    print(f"Mode: {s.mode}   (real sends ready: {s.real_sends_ready()})\n")

    def website():
        from sprinto_agent.research.fetcher import HttpFetcher
        r = HttpFetcher(s, v.policy).fetch("https://example.com/")
        if not r.ok:
            raise RuntimeError(f"{r.status}: {r.error}")
        return f"fetched {len(r.text)} chars, title={r.title!r}"

    def gemini_check():
        if s.llm_provider != "gemini":
            raise Skip(f"LLM_PROVIDER={s.llm_provider}")
        if not s.gemini_api_key:
            raise Skip("GEMINI_API_KEY not set")
        from pydantic import BaseModel

        from sprinto_agent.llm.gemini_llm import GeminiLLM

        class Probe(BaseModel):
            company: str
            is_software: bool
        from pathlib import Path
        llm = GeminiLLM(s.gemini_api_key, s.gemini_models, s.gemini_search_models,
                        timeout=s.gemini_timeout_seconds, max_models=s.gemini_max_models,
                        free_tier=s.gemini_free_tier, state_path=str(Path(s.db_path).parent / "gemini_quota_state.json"))
        out = llm.structured("probe", "Extract the fields.", {"text": "Chargebee sells subscription billing software."}, Probe)
        q = llm.quota_status()
        return (f"{'FREE TIER; ' if s.gemini_free_tier else ''}chain: {' -> '.join(llm.chain)}; structured output OK via "
                f"{llm.last_model} ({out.company}); news search: {q['search']}")

    def anthropic_check():
        import os
        if s.llm_provider == "gemini":
            raise Skip("LLM_PROVIDER=gemini")
        if not (s.anthropic_api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE")):
            raise Skip("ANTHROPIC_API_KEY not set")
        from sprinto_agent.llm.anthropic_llm import AnthropicLLM
        llm = AnthropicLLM(s.anthropic_api_key, s.llm_model)
        m = llm._call(lambda: llm.client.models.retrieve(s.llm_model))
        return f"key valid; model {m.id} available"

    def hunter_check():
        if not s.hunter_api_key:
            raise Skip("HUNTER_API_KEY not set")
        from sprinto_agent.integrations.hunter import HunterClient
        h = HunterClient(s.hunter_api_key)
        acct = h.account()
        plan = acct.get("plan_name")
        searches = (acct.get("requests") or {}).get("searches") or {}
        out = f"plan={plan}; searches used={searches.get('used')} available={searches.get('available')}"
        try:
            d = h.discover({"industry": {"include": ["Software Development"]}, "limit": 1})
            out += f"; Discover OK (applied filters: {list((d.get('meta') or {}).get('filters', {}))})"
        except IntegrationError as e:
            out += f"; Discover NOT available: {e.message}"
        return out

    def google_check():
        if not s.google_configured:
            raise Skip("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN not set")
        import httpx

        from sprinto_agent.integrations.google_auth import GoogleAuth
        auth = GoogleAuth(s.google_client_id, s.google_client_secret, s.google_refresh_token)
        prof = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=auth.headers(), timeout=15)
        if prof.status_code != 200:
            raise RuntimeError(f"Gmail profile HTTP {prof.status_code} (is gmail.readonly granted?)")
        addr = prof.json().get("emailAddress", "")
        note = "" if (s.gmail_sender_address or "").lower() == addr.lower() else \
            f" — WARNING: GMAIL_SENDER_ADDRESS ({s.gmail_sender_address}) differs from the authorised account"
        from sprinto_agent.integrations.calendar import GoogleCalendar
        busy = GoogleCalendar(s, auth).freebusy(clock.now(), clock.now() + timedelta(hours=1))
        return f"authorised as {addr}{note}; calendar free/busy OK ({len(busy)} busy block(s) next hour)"

    def delivery_check():
        if s.mode != "real_demo":
            raise Skip(f"mode is {s.mode}; real sends are off")
        ok, why = s.real_sends_ready()
        if not ok:
            raise RuntimeError(why)
        return f"all emails and invites will go ONLY to {s.demo_recipient_email}"

    check("Website reader", website)
    check("Gemini (LLM)", gemini_check)
    check("Anthropic (LLM)", anthropic_check)
    check("Hunter", hunter_check)
    check("Google (Gmail + Calendar)", google_check)
    check("Real demo delivery", delivery_check)
    for status, name, detail in results:
        print(f"[{status}] {name:<26} {detail}")
    if any(r[0] == FAIL for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
