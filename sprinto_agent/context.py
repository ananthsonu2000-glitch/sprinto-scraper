"""Builds the per-process application context and picks integrations by mode.

Never silently substitutes simulation for a missing real integration outside
SIMULATION mode: missing credentials produce Unconfigured* adapters that fail with
`setup_required`, which the pipeline turns into a visible paused state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import db
from .config import Mode, Settings, Versions, load_versions
from .errors import ErrorKind, IntegrationError
from .retry import default_sleep


class UnconfiguredLLM:
    simulated = False

    def __init__(self, reason):
        self.reason = reason
        self.label = f"LLM NOT CONFIGURED ({reason})"

    def structured(self, *a, **k):
        raise IntegrationError(ErrorKind.SETUP_REQUIRED, self.reason, "llm")

    web_research = structured


class UnconfiguredHunter:
    simulated = False
    label = "Hunter NOT CONFIGURED"

    def domain_search(self, domain):
        raise IntegrationError(ErrorKind.SETUP_REQUIRED, "HUNTER_API_KEY is not set", "hunter")

    def account(self, *a, **k):
        raise IntegrationError(ErrorKind.SETUP_REQUIRED, "HUNTER_API_KEY is not set", "hunter")

    discover = account


@dataclass
class AppContext:
    conn: Any
    settings: Settings
    versions: Versions
    llm: Any
    fetcher: Any
    hunter: Any
    mailbox: Any
    calendar: Any
    sleep: Callable[[float], None] = default_sleep
    extras: dict = field(default_factory=dict)

    @property
    def policy(self) -> dict:
        return self.versions.policy


def build_context(settings: Settings, conn=None, versions: Versions | None = None) -> AppContext:
    conn = conn or db.connect(settings.db_path)
    db.migrate(conn)
    versions = versions or load_versions()
    mode = settings.mode

    if mode == Mode.SIMULATION:
        from .integrations.calendar import SimCalendar
        from .integrations.hunter import SimulatedHunter
        from .integrations.mailbox import SimMailbox
        from .llm.simulated import SimulatedLLM
        from .research.fetcher import FixtureFetcher
        return AppContext(conn, settings, versions, SimulatedLLM(), FixtureFetcher(settings.fixtures_dir),
                          SimulatedHunter(settings.fixtures_dir), SimMailbox(conn, settings), SimCalendar(conn, settings))

    # dry_run and real_demo use real reads
    from .research.fetcher import HttpFetcher
    fetcher = HttpFetcher(settings, versions.policy)

    import os
    other_creds = any(os.environ.get(k) for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID"))
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            llm = UnconfiguredLLM("GEMINI_API_KEY is not set")
        else:
            from . import store
            from .llm.gemini_llm import GeminiLLM

            def on_event(e, _conn=conn):
                if e["ok"] and not e.get("fell_back_from"):
                    return  # only log fallbacks and failures, to keep the activity log readable
                reason = (f"{e['task']} answered by {e['model']} after fallback from {', '.join(e['fell_back_from'])}"
                          if e["ok"] else f"{e['task']}: every Gemini model failed")
                store.log_activity(_conn, None, "llm_model_fallback", "ok" if e["ok"] else "failed", reason,
                                   tool="llm", details=e)
            try:
                from pathlib import Path
                state = (str(Path(settings.db_path).parent / "gemini_quota_state.json")
                         if settings.db_path != ":memory:" else None)
                llm = GeminiLLM(settings.gemini_api_key, settings.gemini_models, settings.gemini_search_models,
                                timeout=settings.gemini_timeout_seconds, max_models=settings.gemini_max_models,
                                on_event=on_event, free_tier=settings.gemini_free_tier, state_path=state)
            except IntegrationError as e:
                llm = UnconfiguredLLM(e.message)
    elif settings.anthropic_api_key or other_creds:
        try:
            from .llm.anthropic_llm import AnthropicLLM
            llm = AnthropicLLM(settings.anthropic_api_key, settings.llm_model, settings.llm_effort, settings.llm_server_fallback)
        except Exception as e:  # noqa: BLE001 - surface as setup issue, never crash startup
            llm = UnconfiguredLLM(f"Anthropic client could not start: {e}")
    else:
        llm = UnconfiguredLLM("ANTHROPIC_API_KEY is not set")

    if settings.hunter_api_key:
        from .integrations.hunter import HunterClient
        hunter = HunterClient(settings.hunter_api_key, versions.policy["hunter"]["result_limit"])
    else:
        hunter = UnconfiguredHunter()

    google_auth = None
    if settings.google_configured:
        from .integrations.google_auth import GoogleAuth
        google_auth = GoogleAuth(settings.google_client_id, settings.google_client_secret, settings.google_refresh_token)

    from .integrations.calendar import DryRunCalendar, GoogleCalendar, UnconfiguredCalendar
    from .integrations.mailbox import DryRunMailbox, GmailMailbox, UnconfiguredMailbox

    if mode == Mode.DRY_RUN:
        reader = GoogleCalendar(settings, google_auth) if google_auth else None
        mailbox = DryRunMailbox(conn, settings)
        calendar = DryRunCalendar(conn, settings, reader)
    else:  # REAL_DEMO
        ready, why = settings.real_sends_ready()
        mailbox = GmailMailbox(settings, google_auth) if ready else UnconfiguredMailbox(f"Real sends disabled: {why}")
        calendar = GoogleCalendar(settings, google_auth) if (ready and google_auth) else UnconfiguredCalendar(f"Real bookings disabled: {why}")
    return AppContext(conn, settings, versions, llm, fetcher, hunter, mailbox, calendar)


def readiness(ctx: AppContext) -> dict:
    s = ctx.settings
    ready, why = s.real_sends_ready()
    items = {
        "llm": {"label": ctx.llm.label, "simulated": getattr(ctx.llm, "simulated", False),
                "configured": not isinstance(ctx.llm, UnconfiguredLLM),
                **({"model_chain": " → ".join(ctx.llm.chain), "last_model": ctx.llm.last_model,
                    "quota": ctx.llm.quota_status()}
                   if hasattr(ctx.llm, "chain") else {})},
        "website_reader": {"label": ctx.fetcher.label, "browser_fallback": s.browser_fallback},
        "hunter": {"label": ctx.hunter.label, "simulated": getattr(ctx.hunter, "simulated", False),
                   "configured": not isinstance(ctx.hunter, UnconfiguredHunter)},
        "gmail": {"label": ctx.mailbox.label, "simulated": getattr(ctx.mailbox, "simulated", False)},
        "calendar": {"label": ctx.calendar.label, "simulated": getattr(ctx.calendar, "simulated", False)},
        "crm": {"label": "Demo activity log (internal; no external CRM connected)"},
        "real_sends": {"ready": ready, "detail": why},
    }
    for row in ctx.conn.execute("SELECT * FROM integration_status"):
        items.setdefault(row["name"], {})["status"] = row["status"]
        items[row["name"]]["status_detail"] = row["detail"]
    return items
