"""Email delivery and reply ingestion.

GmailMailbox  - real Gmail API (REAL_DEMO only, after readiness checks).
SimMailbox    - SIMULATION store in the local DB.
DryRunMailbox - DRY RUN: records what would be sent; nothing leaves the machine.
UnconfiguredMailbox - real_demo without credentials: every send fails with setup_required.

Every implementation calls enforce_email_recipients() before doing anything.
Outbound messages carry X-Sprinto-Agent / X-Sprinto-Op-Id headers and our own
Message-ID so that (a) replies can be told apart from agent mail in self-addressed
demo threads and (b) uncertain sends can be reconciled by Message-ID.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

from .. import clock
from ..errors import ErrorKind, IntegrationError
from .delivery_guard import enforce_email_recipients, enforce_subject
from .google_auth import GoogleAuth, classify_google

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
AGENT_HEADER = "X-Sprinto-Agent"


@dataclass
class OutboundEmail:
    to: list[str]
    subject: str
    body: str
    op_id: str
    thread_id: str | None = None
    in_reply_to: str | None = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    message_id_header: str = ""

    def __post_init__(self):
        if not self.message_id_header:
            safe = uuid.uuid5(uuid.NAMESPACE_URL, self.op_id).hex
            self.message_id_header = f"<{safe}@sprinto-demo.invalid>"


@dataclass
class SentRef:
    provider_message_id: str
    thread_id: str
    message_id_header: str


@dataclass
class ThreadMessage:
    provider_message_id: str
    from_addr: str
    body: str
    subject: str
    is_agent: bool
    author_kind: str
    message_id_header: str | None
    sent_at: str


class SimMailbox:
    label = "SIMULATED mailbox (local DB; nothing is sent)"
    delivery_label = "SIMULATED"
    simulated = True

    def __init__(self, conn, settings):
        self.conn = conn
        self.settings = settings
        self.fail_next: list[str] = []   # tests: timeout_after_store | timeout_before_store | auth | permanent
        self.lookup_fails = False         # tests: reconciliation lookup itself fails

    def send(self, m: OutboundEmail) -> SentRef:
        enforce_email_recipients(self.settings, m.to, m.cc, m.bcc)
        enforce_subject(m.subject)
        mode = self.fail_next.pop(0) if self.fail_next else None
        if mode == "timeout_before_store":
            raise IntegrationError(ErrorKind.UNCERTAIN, "simulated timeout (not delivered)", "mail")
        if mode == "auth":
            raise IntegrationError(ErrorKind.AUTH, "simulated auth failure", "mail")
        if mode == "permanent":
            raise IntegrationError(ErrorKind.PERMANENT, "simulated permanent failure", "mail")
        thread_id = m.thread_id or f"simthr_{uuid.uuid4().hex[:10]}"
        pid = f"simmsg_{uuid.uuid4().hex[:12]}"
        headers = {AGENT_HEADER: "1", "X-Sprinto-Op-Id": m.op_id, "Message-ID": m.message_id_header,
                   "In-Reply-To": m.in_reply_to}
        self.conn.execute(
            "INSERT INTO sim_mailbox(provider_message_id,message_id_header,thread_id,from_addr,to_addr,cc,bcc,subject,body,headers,author_kind,label,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, m.message_id_header, thread_id, "agent@sprinto-demo.invalid", ",".join(m.to), ",".join(m.cc),
             ",".join(m.bcc), m.subject, m.body, json.dumps(headers), "agent", self.delivery_label, clock.iso(clock.now())))
        if mode == "timeout_after_store":
            raise IntegrationError(ErrorKind.UNCERTAIN, "simulated timeout (actually delivered)", "mail")
        return SentRef(pid, thread_id, m.message_id_header)

    def find_sent(self, message_id_header: str) -> SentRef | None:
        if self.lookup_fails:
            raise IntegrationError(ErrorKind.TRANSIENT, "simulated reconciliation lookup failure", "mail")
        r = self.conn.execute("SELECT * FROM sim_mailbox WHERE message_id_header=?", (message_id_header,)).fetchone()
        return SentRef(r["provider_message_id"], r["thread_id"], message_id_header) if r else None

    def list_thread(self, thread_id: str) -> list[ThreadMessage]:
        rows = self.conn.execute("SELECT * FROM sim_mailbox WHERE thread_id=? ORDER BY id", (thread_id,)).fetchall()
        out = []
        for r in rows:
            h = json.loads(r["headers"])
            out.append(ThreadMessage(r["provider_message_id"], r["from_addr"], r["body"], r["subject"],
                                     h.get(AGENT_HEADER) == "1", r["author_kind"], r["message_id_header"], r["created_at"]))
        return out

    def inject_prospect_reply(self, thread_id: str, body: str, subject: str = "Re:") -> str:
        """Labelled SIMULATED prospect reply (testing aid; never a real Gmail message)."""
        pid = f"simreply_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO sim_mailbox(provider_message_id,message_id_header,thread_id,from_addr,to_addr,subject,body,headers,author_kind,label,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, f"<{pid}@simulated-prospect.invalid>", thread_id, "simulated-prospect@simulation.invalid",
             self.settings.delivery_address, subject, body, "{}", "simulated_prospect", "SIMULATED PROSPECT REPLY",
             clock.iso(clock.now())))
        return pid


class DryRunMailbox(SimMailbox):
    label = "DRY RUN mailbox (recorded locally; NOT sent)"
    delivery_label = "DRY RUN — NOT SENT"


class UnconfiguredMailbox:
    label = "Gmail NOT CONFIGURED"
    delivery_label = "NOT SENT"
    simulated = False

    def __init__(self, reason: str):
        self.reason = reason

    def send(self, m):
        raise IntegrationError(ErrorKind.SETUP_REQUIRED, self.reason, "mail")

    def find_sent(self, h):
        return None

    def list_thread(self, t):
        raise IntegrationError(ErrorKind.SETUP_REQUIRED, self.reason, "mail")


class GmailMailbox:
    label = "Gmail (REAL — demo recipient only)"
    delivery_label = "REAL DEMO EMAIL"
    simulated = False

    def __init__(self, settings, auth: GoogleAuth, http: httpx.Client | None = None):
        self.settings = settings
        self.auth = auth
        self.http = http or httpx.Client(timeout=20)

    def send(self, m: OutboundEmail) -> SentRef:
        to = enforce_email_recipients(self.settings, m.to, m.cc, m.bcc)  # boundary check before any I/O
        enforce_subject(m.subject)
        msg = EmailMessage()
        msg["From"] = self.settings.gmail_sender_address
        msg["To"] = to
        msg["Subject"] = m.subject
        msg["Message-ID"] = m.message_id_header
        msg[AGENT_HEADER] = "1"
        msg["X-Sprinto-Op-Id"] = m.op_id
        if m.in_reply_to:
            msg["In-Reply-To"] = m.in_reply_to
            msg["References"] = m.in_reply_to
        msg.set_content(m.body)
        assert msg["Cc"] is None and msg["Bcc"] is None
        payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if m.thread_id:
            payload["threadId"] = m.thread_id
        try:
            r = self.http.post(f"{GMAIL}/messages/send", json=payload, headers=self.auth.headers())
        except httpx.TimeoutException as e:
            raise IntegrationError(ErrorKind.UNCERTAIN, f"Gmail send timed out: {e}", "mail")
        except httpx.TransportError as e:
            raise IntegrationError(ErrorKind.UNCERTAIN, f"Gmail transport error: {e}", "mail")
        if r.status_code != 200:
            raise classify_google(r, "mail", irreversible=True)
        d = r.json()
        return SentRef(d["id"], d["threadId"], m.message_id_header)

    def _get(self, url, params=None):
        try:
            r = self.http.get(url, params=params, headers=self.auth.headers())
        except httpx.HTTPError as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"Gmail read error: {e}", "mail")
        if r.status_code != 200:
            raise classify_google(r, "mail")
        return r.json()

    def find_sent(self, message_id_header: str) -> SentRef | None:
        d = self._get(f"{GMAIL}/messages", {"q": f"rfc822msgid:{message_id_header.strip('<>')}", "includeSpamTrash": "true"})
        msgs = d.get("messages") or []
        if not msgs:
            return None
        return SentRef(msgs[0]["id"], msgs[0]["threadId"], message_id_header)

    def list_thread(self, thread_id: str) -> list[ThreadMessage]:
        d = self._get(f"{GMAIL}/threads/{thread_id}", {"format": "full"})
        out = []
        for msg in d.get("messages", []):
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            is_agent = headers.get(AGENT_HEADER.lower()) == "1"
            body = _plain_text(msg.get("payload", {}))
            if not is_agent:
                body = strip_quoted_reply(body)  # judge only what the person newly wrote
            ts = clock.iso(clock.now())
            if msg.get("internalDate"):
                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc).isoformat()
            out.append(ThreadMessage(msg["id"], headers.get("from", ""), body, headers.get("subject", ""),
                                     is_agent, "agent" if is_agent else "user", headers.get("message-id"), ts))
        return out


_QUOTE_HEADER = re.compile(
    r"^\s*(On .{3,200}wrote:|-{2,}\s*Original Message\s*-{2,}|From:\s.+|Sent from my \w+|_{5,})\s*$",
    re.I | re.M)


def strip_quoted_reply(body: str) -> str:
    """Keep only the newly written part of a reply: drop '>' quoted lines and everything from
    the first quote header ("On <date>, <name> wrote:", "-----Original Message-----", ...)."""
    if not body:
        return body
    text = body.replace("\r\n", "\n")
    m = _QUOTE_HEADER.search(text)
    if m:
        text = text[: m.start()]
    # Gmail sometimes wraps "On ... wrote:" across two lines
    text = re.split(r"\nOn [^\n]{3,200}\n[^\n]{0,200}wrote:\s*\n", "\n" + text)[0]
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    out = "\n".join(lines).strip()
    return out or body.strip()


def _plain_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        t = _plain_text(part)
        if t:
            return t
    return ""
