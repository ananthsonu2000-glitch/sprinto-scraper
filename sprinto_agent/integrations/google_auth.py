"""OAuth refresh-token flow for Gmail + Calendar (server-side only).

Scopes requested by scripts/google_oauth.py (least privilege for implemented features):
  https://www.googleapis.com/auth/gmail.send       - send demo emails
  https://www.googleapis.com/auth/gmail.readonly   - read replies in tracked threads, reconcile sends
  https://www.googleapis.com/auth/calendar.events  - create/update demo events, reconcile bookings
  https://www.googleapis.com/auth/calendar.freebusy - availability checks
"""
from __future__ import annotations

import time

import httpx

from ..errors import ErrorKind, IntegrationError

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]


class GoogleAuth:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, http: httpx.Client | None = None):
        self._cid, self._secret, self._refresh = client_id, client_secret, refresh_token
        self._http = http or httpx.Client(timeout=15)
        self._token: str | None = None
        self._exp = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        try:
            r = self._http.post(TOKEN_URL, data={
                "client_id": self._cid, "client_secret": self._secret,
                "refresh_token": self._refresh, "grant_type": "refresh_token"})
        except httpx.HTTPError as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"token refresh failed: {e}", "google")
        if r.status_code in (400, 401):
            raise IntegrationError(ErrorKind.AUTH, "Google refresh token rejected; re-run scripts/google_oauth.py", "google")
        if r.status_code != 200:
            raise IntegrationError(ErrorKind.TRANSIENT, f"token endpoint HTTP {r.status_code}", "google")
        d = r.json()
        self._token = d["access_token"]
        self._exp = time.time() + int(d.get("expires_in", 3600))
        return self._token

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}


def classify_google(resp: httpx.Response, integration: str, irreversible: bool = False) -> IntegrationError:
    code = resp.status_code
    if code in (401, 403):
        return IntegrationError(ErrorKind.AUTH, f"{integration} permission/auth error HTTP {code}", integration)
    if code in (400, 404, 409, 422):
        return IntegrationError(ErrorKind.INVALID_INPUT, f"{integration} rejected request HTTP {code}: {resp.text[:200]}", integration)
    if code == 429:
        return IntegrationError(ErrorKind.TRANSIENT, f"{integration} rate limited", integration)
    if code >= 500:
        kind = ErrorKind.UNCERTAIN if irreversible else ErrorKind.TRANSIENT
        return IntegrationError(kind, f"{integration} server error HTTP {code}", integration)
    return IntegrationError(ErrorKind.PERMANENT, f"{integration} HTTP {code}", integration)
