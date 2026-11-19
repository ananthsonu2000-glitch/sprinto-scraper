-- Human approval gate: every outgoing email waits here until a person approves it.
CREATE TABLE outbox (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    op_id TEXT NOT NULL UNIQUE,          -- same idempotency key the send ledger uses
    purpose TEXT NOT NULL,               -- initial_outreach | propose_times | booking_confirmation | ...
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    original_body TEXT NOT NULL,         -- what the agent wrote, kept for comparison after edits
    status TEXT NOT NULL,                -- pending | sent | discarded | failed
    context TEXT NOT NULL DEFAULT '{}',  -- why the agent wrote it (reply decision, slots, ...)
    error TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_note TEXT
);
CREATE INDEX idx_outbox_status ON outbox(status);
