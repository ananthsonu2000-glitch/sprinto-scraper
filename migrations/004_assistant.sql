-- Project assistant: chat history, actions waiting for the user's confirmation, small app state.
CREATE TABLE assistant_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,                 -- user | assistant
    content TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',    -- links, actions, tools used
    created_at TEXT NOT NULL
);
CREATE INDEX idx_assistant_messages_conv ON assistant_messages(conversation_id, id);

CREATE TABLE assistant_actions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL,
    status TEXT NOT NULL,               -- pending | done | failed | cancelled
    result TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
