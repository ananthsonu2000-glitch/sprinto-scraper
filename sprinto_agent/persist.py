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
_etag: str | None = None  # version of the main DB snapshot this instance last loaded or wrote


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
    """Main DB last: its snapshot version marks a complete generation."""
    db = Path(os.environ["DB_PATH"])
    return [Path(os.environ["CHECKPOINT_DB_PATH"]), db.parent / "gemini_quota_state.json", db]


def _url(name: str) -> str:
    return f"https://{_store_id()}.{_access()}.blob.vercel-storage.com/{PREFIX}{name}"


def _norm(etag: str | None) -> str | None:
    return etag.removeprefix("W/").strip('"') if etag else None


def _get(http, name: str, etag: str | None = None) -> httpx.Response:
    params = {"cache": "0"} if _access() == "private" else {"t": os.urandom(4).hex()}
    headers = {"authorization": f"Bearer {_token()}"}
    if etag:
        headers["if-none-match"] = f'"{etag}"'
    return http.get(_url(name), params=params, headers=headers)


def restore() -> list[str]:
    """Download the latest snapshot into place (only files not already present)."""
    global _etag
    if not enabled():
        return []
    got = []
    with httpx.Client(timeout=30) as http:
        for f in _files():
            if f.exists():
                continue
            r = _get(http, f.name)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(r.content)
            got.append(f.name)
            if f == _files()[-1]:
                _etag = _norm(r.headers.get("etag"))
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
    global _etag
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
                    if f == _files()[-1]:
                        _etag = _norm((r.json() or {}).get("etag") or _get(http, f.name).headers.get("etag"))
        except Exception:  # noqa: BLE001
            log.exception("snapshot upload failed")


def is_stale() -> bool:
    """True if another instance saved a newer snapshot than the one this instance holds."""
    if not enabled():
        return False
    try:
        with httpx.Client(timeout=15) as http:
            r = _get(http, _files()[-1].name, _etag)
        if r.status_code in (304, 404):
            return False
        return r.status_code == 200 and _norm(r.headers.get("etag")) != _etag
    except Exception:  # noqa: BLE001 - never block a request on the freshness check
        log.exception("freshness check failed")
        return False


def wipe_local() -> None:
    """Drop the local copies so the next restore() pulls the latest snapshot."""
    global _etag
    for f in _files():
        for extra in ("", "-wal", "-shm", "-journal"):
            Path(str(f) + extra).unlink(missing_ok=True)
    _etag = None
