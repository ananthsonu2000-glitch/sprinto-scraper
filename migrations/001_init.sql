-- 001_init: core schema for the Sprinto prospecting MVP

CREATE TABLE campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    capability_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE prospects (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    normalized_domain TEXT NOT NULL,
    mode TEXT NOT NULL,
    stage TEXT NOT NULL,
    paused_stage TEXT,
    terminal_status TEXT,
    company_profile TEXT NOT NULL DEFAULT '{}',
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    rubric_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    capability_version TEXT NOT NULL,
    research_rounds INTEGER NOT NULL DEFAULT 0,
    research_log TEXT NOT NULL DEFAULT '[]',
    qualification_score REAL,
    qualification_status TEXT,
    qualification_detail TEXT NOT NULL DEFAULT '{}',
    contact TEXT,
    contact_score INTEGER,
    contact_candidates TEXT NOT NULL DEFAULT '[]',
    referrals TEXT NOT NULL DEFAULT '[]',
    pain_hypotheses TEXT NOT NULL DEFAULT '[]',
    pain_detail TEXT NOT NULL DEFAULT '{}',
    draft TEXT,
    email_qa TEXT,
    gmail_thread_id TEXT,
    outbound_message_ids TEXT NOT NULL DEFAULT '[]',
    last_processed_message_id TEXT,
    reply_status TEXT,
    reply_round INTEGER NOT NULL DEFAULT 0,
    calendar_checked INTEGER NOT NULL DEFAULT 0,
    calendar_checked_at TEXT,
    checked_slot TEXT,
    times_proposed TEXT NOT NULL DEFAULT '[]',
    rejected_slots TEXT NOT NULL DEFAULT '[]',
    requested_slot TEXT,
    confirmed_slot TEXT,
    organizer_timezone TEXT NOT NULL,
    prospect_timezone TEXT,
    meeting_status TEXT,
    calendar_event_id TEXT,
    crm_logged INTEGER NOT NULL DEFAULT 0,
    reply_deadline TEXT,
    tool_attempts TEXT NOT NULL DEFAULT '{}',
    escalation_reason TEXT,
    suppressed INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    lease_owner TEXT,
    lease_until TEXT,
    UNIQUE (campaign_id, normalized_domain)
);
CREATE INDEX idx_prospects_stage ON prospects(stage);

CREATE TABLE pages (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    url TEXT NOT NULL,
    final_url TEXT,
    status TEXT NOT NULL,
    fetch_method TEXT,
    http_status INTEGER,
    title TEXT,
    text TEXT,
    links TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    research_round INTEGER NOT NULL DEFAULT 0,
    selection_reason TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE (prospect_id, url)
);

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    page_id TEXT REFERENCES pages(id),
    source_url TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    published_at TEXT,
    source_type TEXT NOT NULL,
    field TEXT,
    UNIQUE (prospect_id, source_url, excerpt)
);

CREATE TABLE hunter_cache (
    domain TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE operations (
    op_id TEXT PRIMARY KEY,
    prospect_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending | succeeded | uncertain | failed_retryable | failed_permanent
    request TEXT NOT NULL DEFAULT '{}',
    provider_ref TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    provider_message_id TEXT NOT NULL UNIQUE,
    thread_id TEXT,
    direction TEXT NOT NULL,       -- outbound | inbound
    author_kind TEXT NOT NULL,     -- agent | user | simulated_prospect | system_notice
    subject TEXT,
    body TEXT,
    sent_at TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'n/a',  -- pending | processing | done | skipped | n/a
    decision TEXT,
    graph_thread_id TEXT,
    delivery_label TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_messages_prospect ON messages(prospect_id, processing_status);

CREATE TABLE activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    campaign_id TEXT,
    prospect_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    tool TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_activity_prospect ON activity_log(prospect_id, id);

CREATE TABLE escalations (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id),
    reason TEXT NOT NULL,
    details TEXT,
    status TEXT NOT NULL,          -- open | resolved
    allowed_actions TEXT NOT NULL DEFAULT '[]',
    resolution TEXT,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE integration_status (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- ok | paused_quota | auth_failed | setup_required
    detail TEXT,
    updated_at TEXT NOT NULL
);

-- Simulation-only stores (clearly separate from real providers)
CREATE TABLE sim_mailbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_message_id TEXT NOT NULL UNIQUE,
    message_id_header TEXT,
    thread_id TEXT NOT NULL,
    from_addr TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    cc TEXT NOT NULL DEFAULT '',
    bcc TEXT NOT NULL DEFAULT '',
    subject TEXT,
    body TEXT,
    headers TEXT NOT NULL DEFAULT '{}',
    author_kind TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE sim_calendar_busy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    label TEXT
);

CREATE TABLE sim_calendar_events (
    event_id TEXT PRIMARY KEY,
    op_id TEXT,
    summary TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    attendees TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
