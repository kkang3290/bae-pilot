PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS conversations (
 scope TEXT PRIMARY KEY, channel TEXT NOT NULL, account_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL, mode TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 last_processed INTEGER NOT NULL DEFAULT 0, memory_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
 record_id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL REFERENCES conversations(scope),
 event_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
 direction TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, received_at REAL NOT NULL,
 UNIQUE(scope, event_id)
);
CREATE INDEX IF NOT EXISTS events_scope ON events(scope, record_id);
CREATE TABLE IF NOT EXISTS controls (
 scope TEXT PRIMARY KEY, mode TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO controls VALUES ('*', 'auto', 0);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES conversations(scope),
 revision INTEGER NOT NULL, last_event_id INTEGER NOT NULL, status TEXT NOT NULL,
 result TEXT, error TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_scope ON runs(scope, created_at);
CREATE TABLE IF NOT EXISTS jobs (
 job_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
 scope TEXT NOT NULL REFERENCES conversations(scope), status TEXT NOT NULL,
 due_at REAL NOT NULL, first_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 payload TEXT NOT NULL DEFAULT '{}', error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_job ON jobs(kind, scope) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status, due_at);
CREATE TABLE IF NOT EXISTS outbox (
 send_id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES conversations(scope),
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id), source_revision INTEGER NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, not_before REAL NOT NULL, sent_at REAL, external_message_id TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_scope ON outbox(scope, created_at);
CREATE TABLE IF NOT EXISTS memories (
 memory_id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL REFERENCES conversations(scope),
 kind TEXT NOT NULL, content TEXT NOT NULL, source_event_ids TEXT NOT NULL,
 source_version INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
 audit_id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, action TEXT NOT NULL,
 details TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mock_receipts (
 send_id TEXT PRIMARY KEY, scope TEXT NOT NULL, text TEXT NOT NULL,
 external_message_id TEXT NOT NULL, created_at REAL NOT NULL
);
PRAGMA user_version = 1;
