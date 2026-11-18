"""Background worker: advances outreach, polls tracked threads, runs the reply agent,
and enforces the no-reply deadline. Safe to run several instances: every prospect
is processed under a DB lease, so two workers never handle the same conversation.

Usage:
  python -m sprinto_agent.worker --once      # single tick
  python -m sprinto_agent.worker             # loop every WORKER_POLL_SECONDS
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import time
import uuid

from . import clock, store
from .config import Settings
from .context import build_context
from .errors import IntegrationError
from .outreach import process_prospect
from .reply_agent import process_pending_messages
from .store import Stage
from .tools import run_tool

log = logging.getLogger("worker")


class Worker:
    def __init__(self, ctx, owner: str | None = None):
        self.ctx = ctx
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lease_s = ctx.policy["leases"]["prospect_lease_seconds"]

    def _with_lease(self, pid, fn):
        if not store.acquire_lease(self.ctx.conn, pid, self.owner, self.lease_s):
            return None
        try:
            return fn()
        finally:
            store.release_lease(self.ctx.conn, pid, self.owner)

    def tick(self) -> dict:
        c = self.ctx.conn
        stats = {"outreach": 0, "polled": 0, "replies_processed": 0, "deadlines": 0}
        store.replay_fallback_log(c)
        hunter = store.get_integration_status(c, "hunter")
        hunter_paused = bool(hunter and hunter["status"] in ("paused_quota", "auth_failed", "setup_required"))

        # 1) outreach stages
        active = [r["id"] for r in c.execute(
            f"SELECT id, stage FROM prospects WHERE terminal_status IS NULL AND stage IN ({','.join('?' * len(Stage.OUTREACH_ACTIVE))})"
            " ORDER BY created_at", Stage.OUTREACH_ACTIVE)
            if not (hunter_paused and r["stage"] == Stage.QUALIFIED)]
        for pid in active:
            if self._with_lease(pid, lambda: process_prospect(self.ctx, pid)) is not None:
                stats["outreach"] += 1

        # 2) poll tracked threads (only campaign threads; never the wider inbox)
        conv = [r["id"] for r in c.execute(
            "SELECT id FROM prospects WHERE gmail_thread_id IS NOT NULL AND suppressed=0 AND stage IN (?,?)",
            Stage.CONVERSATION)]
        for pid in conv:
            if self._with_lease(pid, lambda: run_tool(self.ctx, "read_thread_replies", {"prospect_id": pid})) is not None:
                stats["polled"] += 1

        # 3) process pending replies through the LangGraph agent
        pend = [r["prospect_id"] for r in c.execute(
            "SELECT DISTINCT prospect_id FROM messages WHERE direction='inbound' AND processing_status IN ('pending','processing')")]
        for pid in pend:
            def run_replies(pid=pid):
                try:
                    return process_pending_messages(self.ctx, pid)
                except IntegrationError as e:
                    # e.g. free AI quota used up: the reply stays queued and resumes from its
                    # checkpoint on a later tick; other conversations keep going.
                    store.log_activity(c, pid, "reply_agent", "waiting_for_quota" if e.kind == "quota" else "retry_later",
                                       e.message[:300])
                    return []
            out = self._with_lease(pid, run_replies)
            if out:
                stats["replies_processed"] += len(out)

        # 4) no-reply deadline -> handoff (no automatic chase emails)
        now = clock.iso(clock.now())
        due = [r["id"] for r in c.execute(
            "SELECT id FROM prospects WHERE stage IN (?,?) AND reply_deadline IS NOT NULL AND reply_deadline < ? "
            "AND reply_status NOT IN ('MEETING_BOOKED') AND NOT EXISTS (SELECT 1 FROM messages m WHERE "
            "m.prospect_id=prospects.id AND m.direction='inbound' AND m.processing_status IN ('pending','processing'))",
            (*Stage.CONVERSATION, now))]
        for pid in due:
            def fire(pid=pid):
                p = store.get_prospect(c, pid)
                if p["stage"] in Stage.CONVERSATION and p["meeting_status"] not in ("BOOKED", "RESCHEDULED"):
                    store.open_escalation(c, p, "NO_REPLY",
                                          f"No reply within {self.ctx.settings.no_reply_handoff_hours}h after last message; no automatic chase sent")
                    return True
            if self._with_lease(pid, fire):
                stats["deadlines"] += 1
        return stats

    def run_forever(self):
        while True:
            try:
                stats = self.tick()
                log.info("tick %s", stats)
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            time.sleep(self.ctx.settings.worker_poll_seconds)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    ctx = build_context(Settings.from_env())
    w = Worker(ctx)
    if args.once:
        print(w.tick())
    else:
        w.run_forever()


if __name__ == "__main__":
    main()
