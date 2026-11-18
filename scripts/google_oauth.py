"""One-time helper to obtain a Google OAuth refresh token (Gmail + Calendar).

1. In Google Cloud Console create an OAuth client of type "Desktop app";
   enable the Gmail API and Google Calendar API.
2. Put GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env.
3. Run:  python scripts/google_oauth.py
   A browser opens; sign in as the DEMO account and approve the scopes.
4. Copy the printed GOOGLE_REFRESH_TOKEN into .env (never commit it).

Scopes (least privilege for implemented features): gmail.send, gmail.readonly,
calendar.events, calendar.freebusy.
"""
from __future__ import annotations

import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sprinto_agent.config import ROOT, load_dotenv  # noqa: E402
from sprinto_agent.integrations.google_auth import SCOPES, TOKEN_URL  # noqa: E402

PORT = 8799


def main():
    load_dotenv(ROOT / ".env")
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.")
    state = secrets.token_urlsafe(16)
    redirect = f"http://127.0.0.1:{PORT}/"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state})
    result: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    print("Consent URL (open it and sign in as the demo account):\n" + url, flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    t.join(timeout=900)
    if result.get("state") != state or "code" not in result:
        sys.exit(f"OAuth failed: {result.get('error', 'state mismatch')}")
    r = httpx.post(TOKEN_URL, data={"code": result["code"], "client_id": cid, "client_secret": secret,
                                    "redirect_uri": redirect, "grant_type": "authorization_code"})
    r.raise_for_status()
    tok = r.json().get("refresh_token")
    if not tok:
        sys.exit("No refresh token returned (revoke prior consent and retry).")
    # Save straight into .env so the token never has to be copied by hand.
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    out, done = [], False
    for line in lines:
        if line.startswith("GOOGLE_REFRESH_TOKEN="):
            out.append("GOOGLE_REFRESH_TOKEN=" + tok)
            done = True
        else:
            out.append(line)
    if not done:
        out.append("GOOGLE_REFRESH_TOKEN=" + tok)
    env.write_text("\n".join(out) + "\n", encoding="utf-8")
    # Which account was authorised (so GMAIL_SENDER_ADDRESS can be checked)
    access = r.json().get("access_token")
    who = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                    headers={"Authorization": f"Bearer {access}"}, timeout=15)
    addr = who.json().get("emailAddress") if who.status_code == 200 else "unknown"
    print(f"\nSaved GOOGLE_REFRESH_TOKEN to .env. Authorised account: {addr}")
    print("Set GMAIL_SENDER_ADDRESS to that address, then run: python scripts/check_setup.py")


if __name__ == "__main__":
    main()
