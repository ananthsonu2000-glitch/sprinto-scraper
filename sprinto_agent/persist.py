"""Durable state for serverless hosting (Vercel): snapshot the SQLite files to Vercel Blob.

Serverless functions only have an ephemeral /tmp disk, so on a cold start we download the
last snapshot, and after every unit of work we upload a fresh one. Uses the Blob REST API
directly (no extra dependency). Enabled only when BLOB_READ_WRITE_TOKEN is set; locally it
is a no-op and the normal data/ folder is used.

Blob store access defaults to "private" (the database holds names, emails and calendar
data); set BLOB_ACCESS=public only if the store was created as public.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

import httpx

log = logging.getLogger("persist")
API = os.environ.get("VERCEL_BLOB_API_URL") or "https://vercel.com/api/blob"
API_VERSION = "13"
PREFIX = "sprinto-state/"
_lock = threading.Lock()


def enabled() -> bool:
    return bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))


def _token() -> str:
    return os.environ["BLOB_READ_WRITE_TOKEN"]


def _access() -> str:
    return (os.environ.get("BLOB_ACCESS") or "private").strip().lower()


def _store_id() -> str:
    sid = os.environ.get("BLOB_STORE_ID") or _token().split("_")[3]
    return sid.removeprefix("store_")


def _files() -> list[Path]:
    db = Path(os.environ["DB_PATH"])
    return [db, Path(os.environ["CHECKPOINT_DB_PATH"]), db.parent / "gemini_quota_state.json"]


def restore() -> list[str]:
    """Download the latest snapshot into place (only files not already present)."""
    if not enabled():
        return []
    got = []
    with httpx.Client(timeout=30) as http:
        for f in _files():
            if f.exists():
                continue
            url = f"https://{_store_id()}.{_access()}.blob.vercel-storage.com/{PREFIX}{f.name}"
            params = {"cache": "0"} if _access() == "private" else {"t": os.urandom(4).hex()}
            r = http.get(url, params=params, headers={"authorization": f"Bearer {_token()}"})
            if r.status_code == 404:
                continue
            r.raise_for_status()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(r.content)
            got.append(f.name)
    log.info("restored %s", got)
    return got


def _snapshot_bytes(f: Path) -> bytes:
    if f.suffix != ".db":
        return f.read_bytes()
    # sqlite backup API gives a consistent copy that includes committed WAL pages
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / f.name
        src, dst = sqlite3.connect(f), sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return out.read_bytes()


def save() -> None:
    """Upload a snapshot of every state file. Failures are logged, never raised."""
    if not enabled():
        return
    with _lock:
        try:
            with httpx.Client(timeout=60) as http:
                for f in _files():
                    if not f.exists():
                        continue
                    r = http.put(
                        f"{API}/?{urlencode({'pathname': PREFIX + f.name})}",
                        content=_snapshot_bytes(f),
                        headers={
                            "authorization": f"Bearer {_token()}",
                            "x-api-version": API_VERSION,
                            "x-vercel-blob-store-id": _store_id(),
                            "x-vercel-blob-access": _access(),
                            "x-add-random-suffix": "0",
                            "x-allow-overwrite": "1",
                            "x-cache-control-max-age": "60",
                            "x-content-type": "application/octet-stream",
                        },
                    )
                    r.raise_for_status()
        except Exception:  # noqa: BLE001
            log.exception("snapshot upload failed")
