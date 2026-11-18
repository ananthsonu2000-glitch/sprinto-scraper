"""Vercel entrypoint: the real app (live by default), configured from Vercel env vars.

Serverless differences from running locally (handled automatically):
  * state lives in /tmp and is snapshotted to Vercel Blob (BLOB_READ_WRITE_TOKEN), so it
    survives cold starts;
  * there is no background worker: the dashboard runs worker ticks while it is open
    ("Auto-run worker"), each tick is time-boxed and saves after every unit of work;
  * no headless browser fallback (Chromium does not fit in a function).

Set APP_MODE=simulation in Vercel to host the fixture demo instead.
"""
import os

os.environ.setdefault("APP_MODE", "real_demo")
os.environ.setdefault("SERVERLESS", "1")
os.environ.setdefault("DB_PATH", "/tmp/sprinto/sprinto.db")
os.environ.setdefault("CHECKPOINT_DB_PATH", "/tmp/sprinto/checkpoints.db")
os.environ.setdefault("GEMINI_TIMEOUT_SECONDS", "45")
os.environ["EMBEDDED_WORKER"] = "false"   # no background threads on serverless
os.environ["BROWSER_FALLBACK"] = "false"  # no Chromium on serverless

from sprinto_agent.api import app  # noqa: E402,F401
