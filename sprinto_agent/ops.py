"""Operation ledger for irreversible side effects (send email, create/update event).

Stable operation IDs:
  initial outreach : outreach:{campaign_id}:{domain}
  reply send       : reply:{thread_id}:{incoming_message_id}:{action}
  booking          : book:{prospect_id}:{slot_iso}

Rules:
  * succeeded op  -> return stored provider ref; never re-execute.
  * pending/uncertain op (e.g. crash or timeout) -> reconcile with the provider first.
      - found      -> mark succeeded
      - confirmed absent -> safe to attempt (bounded)
      - lookup failed -> stays uncertain -> OperationUncertain (caller pauses + escalates)
  * permanent / invalid / auth failure -> failed_permanent; not retried unchanged.
"""
from __future__ import annotations

import json
from typing import Callable

from . import clock
from .errors import ErrorKind, IntegrationError, OperationUncertain


class Ledger:
    def __init__(self, conn, max_reconciled_retries: int = 1):
        self.conn = conn
        self.max_reconciled_retries = max_reconciled_retries

    def get(self, op_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
        return dict(r) if r else None

    def _set(self, op_id, **kw):
        kw["updated_at"] = clock.iso(clock.now())
        cols = ", ".join(f"{k}=?" for k in kw)
        self.conn.execute(f"UPDATE operations SET {cols} WHERE op_id=?", (*kw.values(), op_id))

    def mark(self, op_id: str, status: str, provider_ref: str | None = None, error: str | None = None):
        self._set(op_id, status=status, provider_ref=provider_ref, error=error)

    def run(self, op_id: str, kind: str, prospect_id: str, request: dict,
            do: Callable[[], str], reconcile: Callable[[], str | None]) -> tuple[str, bool]:
        """Returns (provider_ref, executed_now). Raises OperationUncertain or IntegrationError."""
        op = self.get(op_id)
        now = clock.iso(clock.now())
        if op is None:
            self.conn.execute(
                "INSERT INTO operations(op_id,prospect_id,kind,status,request,attempts,created_at,updated_at)"
                " VALUES (?,?,?,?,?,0,?,?)", (op_id, prospect_id, kind, "new", json.dumps(request, default=str), now, now))
            op = self.get(op_id)
        if op["status"] == "succeeded":
            return op["provider_ref"], False
        if op["status"] == "failed_permanent":
            raise IntegrationError(ErrorKind.PERMANENT, f"operation {op_id} previously failed permanently: {op['error']}", kind)
        if op["status"] in ("pending", "uncertain", "failed_retryable"):
            # Always ask the provider first: a previous attempt may have landed.
            ref = self._reconcile(op_id, reconcile)
            if ref:
                return ref, False
            if op["status"] != "failed_retryable" and op["attempts"] > self.max_reconciled_retries:
                self.mark(op_id, "uncertain", error="attempt limit reached after reconciliation")
                raise OperationUncertain(op_id, "attempt limit reached; human must confirm")
        return self._attempt(op_id, do, reconcile)

    def _reconcile(self, op_id, reconcile) -> str | None:
        try:
            ref = reconcile()
        except IntegrationError as e:
            self.mark(op_id, "uncertain", error=f"reconciliation lookup failed: {e.message}")
            raise OperationUncertain(op_id, f"could not verify provider state: {e.message}")
        if ref:
            self.mark(op_id, "succeeded", provider_ref=ref, error=None)
        return ref

    def _attempt(self, op_id, do, reconcile) -> tuple[str, bool]:
        self.conn.execute("UPDATE operations SET status='pending', attempts=attempts+1, updated_at=? WHERE op_id=?",
                          (clock.iso(clock.now()), op_id))
        try:
            ref = do()
        except IntegrationError as e:
            if e.kind in (ErrorKind.UNCERTAIN, ErrorKind.TRANSIENT):
                # The request may have reached the provider. Reconcile before anything else.
                ref = self._reconcile(op_id, reconcile)
                if ref:
                    return ref, True
                if self.get(op_id)["attempts"] <= self.max_reconciled_retries:
                    return self._attempt(op_id, do, reconcile)
                self.mark(op_id, "uncertain", error=e.message)
                raise OperationUncertain(op_id, f"outcome uncertain after reconciliation: {e.message}")
            # auth/setup/quota failures happen before anything is sent: retryable once fixed.
            status = "failed_retryable" if e.kind in (ErrorKind.AUTH, ErrorKind.SETUP_REQUIRED, ErrorKind.QUOTA) \
                else "failed_permanent"
            self.mark(op_id, status, error=f"{e.kind}: {e.message}")
            raise
        self.mark(op_id, "succeeded", provider_ref=ref)
        return ref, True
