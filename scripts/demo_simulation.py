"""End-to-end SIMULATION walkthrough (no network, nothing sent, no real bookings).

  python scripts/demo_simulation.py

Runs the fixture campaign, then plays labelled simulated prospect replies through
the reply agent: interest -> slot confirmation -> booking.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sprinto_agent import clock, store  # noqa: E402
from sprinto_agent.config import Mode, Settings  # noqa: E402
from sprinto_agent.context import build_context  # noqa: E402
from sprinto_agent.outreach import create_campaign  # noqa: E402
from sprinto_agent.worker import Worker  # noqa: E402

DOMAINS = ["acmecloud.example", "brightbakery.example", "certifyfast.example", "quietsaas.example",
           "nocontact.example", "flakyco.example"]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    clock.set_now(datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc))
    d = Path(tempfile.mkdtemp())
    s = Settings(mode=Mode.SIMULATION, db_path=str(d / "sim.db"), checkpoint_db_path=str(d / "cp.db"),
                 demo_recipient_email="demo.inbox@example.com", retry_backoff_override=[0, 0])
    ctx = build_context(s)
    print("MODE:", Mode.LABELS[s.mode])
    r = create_campaign(ctx, "simulation demo", DOMAINS + ["localhost", "ACMECLOUD.example/about"])
    print("rejected inputs:", r["rejected"])
    w = Worker(ctx)
    print("tick:", w.tick())
    for pid in r["prospect_ids"]:
        p = store.get_prospect(ctx.conn, pid)
        print(f"- {p['normalized_domain']:<22} stage={p['stage']:<16} outcome={p['terminal_status'] or p['reply_status']} "
              f"score={p['qualification_score']} rounds={p['research_rounds']} contact={(p['contact'] or {}).get('name')}")
        if p["escalation_reason"]:
            print("    escalation:", p["escalation_reason"])
    acme = next(store.get_prospect(ctx.conn, x) for x in r["prospect_ids"]
                if store.get_prospect(ctx.conn, x)["normalized_domain"] == "acmecloud.example")
    if acme["draft"]:
        print("\n--- SIMULATED outreach email (to demo address only) ---")
        print(ctx.conn.execute("SELECT to_addr, cc, bcc, subject FROM sim_mailbox ORDER BY id LIMIT 1").fetchone()[:])
        print(acme["draft"]["body"])
        print("QA:", acme["email_qa"]["score"], {k: v["passed"] for k, v in acme["email_qa"]["checks"].items()})
    if acme["gmail_thread_id"]:
        for text in ["Thanks, I'm interested — happy to chat.", "Option B works for me."]:
            print(f"\n>>> SIMULATED PROSPECT REPLY: {text}")
            ctx.mailbox.inject_prospect_reply(acme["gmail_thread_id"], text)
            print("tick:", w.tick())
            p = store.get_prospect(ctx.conn, acme["id"])
            print("reply_status:", p["reply_status"], "meeting:", p["meeting_status"], p["confirmed_slot"])
            last = ctx.conn.execute("SELECT body FROM sim_mailbox WHERE author_kind='agent' ORDER BY id DESC LIMIT 1").fetchone()
            print("agent reply:\n" + last["body"])
        print("\nSIMULATED calendar events:", [dict(e) for e in ctx.conn.execute("SELECT summary,start,attendees,label FROM sim_calendar_events")])
    print("\nopen escalations:", json.dumps([(e["normalized_domain"], e["reason"]) for e in store.open_escalations(ctx.conn)]))


if __name__ == "__main__":
    main()
