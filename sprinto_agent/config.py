"""Settings (env-driven) and versioned configuration loading.

Secrets are read server-side only and never serialized to the frontend:
`Settings.public_view()` reports only whether each secret is configured.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
FIXTURES_DIR = ROOT / "fixtures"


class Mode:
    SIMULATION = "simulation"
    DRY_RUN = "dry_run"
    REAL_DEMO = "real_demo"
    ALL = (SIMULATION, DRY_RUN, REAL_DEMO)

    LABELS = {
        SIMULATION: "SIMULATION — fixtures and simulated integrations; nothing is real",
        DRY_RUN: "DRY RUN — real reads; emails and bookings are recorded, NOT sent",
        REAL_DEMO: "REAL DEMO — real sends/bookings, delivered ONLY to the demo address",
    }


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _bool(v: str | None, default=False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    mode: str = Mode.SIMULATION
    db_path: str = str(ROOT / "data" / "sprinto_agent.db")
    checkpoint_db_path: str = str(ROOT / "data" / "langgraph_checkpoints.db")
    demo_recipient_email: str | None = None
    real_sends_enabled: bool = False
    llm_provider: str = "anthropic"
    gemini_api_key: str | None = None
    gemini_models: list[str] | None = None        # override; default = auto-ranked from your key's model list
    gemini_search_models: list[str] | None = None
    gemini_max_models: int = 10
    gemini_timeout_seconds: float = 90.0
    gemini_free_tier: bool = False  # skip models with no free quota; wait for daily resets instead of erroring
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_server_fallback: str = "default"
    hunter_api_key: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    gmail_sender_address: str | None = None
    google_calendar_id: str = "primary"
    sender_display_name: str = "Demo Sender"
    organizer_timezone: str = "Asia/Kolkata"
    working_hours_start: str = "09:00"
    working_hours_end: str = "18:00"
    meeting_minutes: int = 30
    booking_horizon_days: int = 30
    no_reply_handoff_hours: int = 48
    browser_fallback: bool = False
    fetch_timeout_seconds: float = 10.0
    fetch_max_bytes: int = 2_000_000
    worker_poll_seconds: int = 60
    embedded_worker: bool = False
    fixtures_dir: str = str(FIXTURES_DIR)
    retry_backoff_override: list[float] | None = None  # tests set [0, 0]

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        load_dotenv(ROOT / ".env")
        e = os.environ.get
        s = cls(
            mode=(e("APP_MODE") or Mode.SIMULATION).strip().lower(),
            db_path=e("DB_PATH") or str(ROOT / "data" / "sprinto_agent.db"),
            checkpoint_db_path=e("CHECKPOINT_DB_PATH") or str(ROOT / "data" / "langgraph_checkpoints.db"),
            demo_recipient_email=(e("DEMO_RECIPIENT_EMAIL") or None),
            real_sends_enabled=_bool(e("REAL_SENDS_ENABLED")),
            llm_provider=(e("LLM_PROVIDER") or ("gemini" if e("GEMINI_API_KEY") and not e("ANTHROPIC_API_KEY") else "anthropic")).strip().lower(),
            gemini_api_key=e("GEMINI_API_KEY") or None,
            gemini_models=[m.strip() for m in (e("GEMINI_MODELS") or e("GEMINI_MODEL") or "").split(",") if m.strip()] or None,
            gemini_search_models=[m.strip() for m in (e("GEMINI_SEARCH_MODELS") or "").split(",") if m.strip()] or None,
            gemini_max_models=int(e("GEMINI_MAX_MODELS") or 10),
            gemini_timeout_seconds=float(e("GEMINI_TIMEOUT_SECONDS") or 90),
            gemini_free_tier=_bool(e("GEMINI_FREE_TIER")),
            anthropic_api_key=e("ANTHROPIC_API_KEY") or None,
            llm_model=e("LLM_MODEL") or "claude-opus-5-5",
            llm_effort=e("LLM_EFFORT") or "medium",
            llm_server_fallback=e("LLM_SERVER_FALLBACK") or "default",
            hunter_api_key=e("HUNTER_API_KEY") or None,
            google_client_id=e("GOOGLE_CLIENT_ID") or None,
            google_client_secret=e("GOOGLE_CLIENT_SECRET") or None,
            google_refresh_token=e("GOOGLE_REFRESH_TOKEN") or None,
            gmail_sender_address=e("GMAIL_SENDER_ADDRESS") or None,
            google_calendar_id=e("GOOGLE_CALENDAR_ID") or "primary",
            sender_display_name=e("SENDER_DISPLAY_NAME") or "Demo Sender",
            organizer_timezone=e("ORGANIZER_TIMEZONE") or "Asia/Kolkata",
            working_hours_start=e("WORKING_HOURS_START") or "09:00",
            working_hours_end=e("WORKING_HOURS_END") or "18:00",
            meeting_minutes=int(e("MEETING_MINUTES") or 30),
            booking_horizon_days=int(e("BOOKING_HORIZON_DAYS") or 30),
            no_reply_handoff_hours=int(e("NO_REPLY_HANDOFF_HOURS") or 48),
            browser_fallback=_bool(e("BROWSER_FALLBACK")),
            fetch_timeout_seconds=float(e("FETCH_TIMEOUT_SECONDS") or 10),
            fetch_max_bytes=int(e("FETCH_MAX_BYTES") or 2_000_000),
            worker_poll_seconds=int(e("WORKER_POLL_SECONDS") or 60),
            embedded_worker=_bool(e("EMBEDDED_WORKER")),
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        if s.mode not in Mode.ALL:
            raise ValueError(f"APP_MODE must be one of {Mode.ALL}, got {s.mode!r}")
        return s

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)

    @property
    def delivery_address(self) -> str:
        """The ONLY address mail may go to. Simulation gets a non-routable placeholder if unset."""
        if self.demo_recipient_email:
            return self.demo_recipient_email.strip().lower()
        if self.mode == Mode.SIMULATION:
            return "demo-recipient@simulation.invalid"
        if self.mode == Mode.DRY_RUN:
            return "demo-recipient@dry-run.invalid"  # nothing is ever sent in dry run
        raise RuntimeError("DEMO_RECIPIENT_EMAIL is required in real_demo mode")

    def real_sends_ready(self) -> tuple[bool, str]:
        if self.mode != Mode.REAL_DEMO:
            return False, f"mode is {self.mode}"
        missing = []
        if not self.demo_recipient_email:
            missing.append("DEMO_RECIPIENT_EMAIL")
        if not self.real_sends_enabled:
            missing.append("REAL_SENDS_ENABLED=true")
        if not self.google_configured:
            missing.append("Google OAuth credentials")
        if not self.gmail_sender_address:
            missing.append("GMAIL_SENDER_ADDRESS")
        if missing:
            return False, "missing: " + ", ".join(missing)
        return True, "ready"

    def public_view(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": Mode.LABELS[self.mode],
            "demo_recipient_configured": bool(self.demo_recipient_email),
            "demo_recipient": self.demo_recipient_email,  # user's own demo address, not a secret
            "real_sends_enabled_flag": self.real_sends_enabled,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model if self.llm_provider == "anthropic" else "auto-ranked Gemini chain",
            "organizer_timezone": self.organizer_timezone,
            "working_hours": f"{self.working_hours_start}-{self.working_hours_end} (Mon-Fri)",
            "meeting_minutes": self.meeting_minutes,
            "booking_horizon_days": self.booking_horizon_days,
            "no_reply_handoff_hours": self.no_reply_handoff_hours,
        }


@dataclass
class Versions:
    rubric: dict
    policy: dict
    capabilities: dict
    prompts: dict
    hashes: dict = field(default_factory=dict)

    @property
    def rubric_version(self) -> str:
        return self.rubric["version"]

    @property
    def policy_version(self) -> str:
        return self.policy["version"]

    @property
    def capability_version(self) -> str:
        return self.capabilities["version"]

    @property
    def prompt_version(self) -> str:
        return self.prompts["version"]

    def capability_ids(self) -> set[str]:
        return {c["id"] for c in self.capabilities["capabilities"]}

    def capability(self, cid: str) -> dict | None:
        return next((c for c in self.capabilities["capabilities"] if c["id"] == cid), None)

    def system_prompt(self, task: str) -> str:
        return self.prompts["shared_preamble"] + "\n\nTASK:\n" + self.prompts["tasks"][task]

    def summary(self) -> dict:
        return {
            "rubric_version": self.rubric_version,
            "policy_version": self.policy_version,
            "capability_version": self.capability_version,
            "prompt_version": self.prompt_version,
            "hashes": self.hashes,
        }


def load_versions(config_dir: Path = CONFIG_DIR) -> Versions:
    def load(name):
        raw = (config_dir / name).read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()[:12]

    rubric, h1 = load("rubric_v1.json")
    policy, h2 = load("policy_v1.json")
    caps, h3 = load("capabilities_v1.json")
    prompts, h4 = load("prompts_v1.json")
    return Versions(rubric, policy, caps, prompts,
                    {"rubric": h1, "policy": h2, "capabilities": h3, "prompts": h4})
