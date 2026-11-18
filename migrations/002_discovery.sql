-- 002_discovery: find companies from filters instead of typing domains

CREATE TABLE discovery_searches (
    id TEXT PRIMARY KEY,
    filters TEXT NOT NULL,
    sources TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,              -- completed | partial | failed
    source_reports TEXT NOT NULL DEFAULT '[]',   -- per-source: ok/error, applied vs ignored filters
    created_at TEXT NOT NULL
);

CREATE TABLE discovery_candidates (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES discovery_searches(id),
    name TEXT,
    domain TEXT,                       -- NULL when no domain could be verified
    sources TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',  -- [{source, url, title, excerpt, published_at, signal}]
    signals TEXT NOT NULL DEFAULT '{}',   -- extracted facts (funding, NRR, headcount...) with evidence refs
    matches TEXT NOT NULL DEFAULT '{}',   -- per filter: matched | unknown | not_matched + basis
    match_level TEXT NOT NULL,         -- strong | partial | excluded | needs_domain
    status TEXT NOT NULL DEFAULT 'new',   -- new | added | dismissed
    campaign_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_discovery_candidates_search ON discovery_candidates(search_id);
