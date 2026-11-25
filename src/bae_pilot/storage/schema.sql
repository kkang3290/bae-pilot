-- PostgreSQL-only schema. Initialization is atomic and guarded by a session advisory lock.
CREATE TABLE schema_version (
 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
 version INTEGER NOT NULL
);
CREATE TABLE conversations (
 scope TEXT PRIMARY KEY, channel TEXT NOT NULL, account_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL, mode TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 0,
 last_processed BIGINT NOT NULL DEFAULT 0, memory_version BIGINT NOT NULL DEFAULT 0,
 UNIQUE(channel, account_id, conversation_id)
);
CREATE TABLE events (
 record_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 scope TEXT NOT NULL REFERENCES conversations(scope),
 event_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload JSONB NOT NULL,
 direction TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
 received_at TIMESTAMPTZ NOT NULL, UNIQUE(scope, event_id)
);
CREATE INDEX events_scope ON events(scope, record_id);
CREATE TABLE controls (
 scope TEXT PRIMARY KEY, mode TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 0
);
INSERT INTO controls VALUES ('*', 'auto', 0);
CREATE TABLE runs (
 run_id TEXT PRIMARY KEY, run_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 scope TEXT NOT NULL REFERENCES conversations(scope),
 revision BIGINT NOT NULL, last_event_id BIGINT NOT NULL, status TEXT NOT NULL,
 result JSONB, error TEXT, created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX runs_scope ON runs(scope, run_seq DESC);
CREATE TABLE jobs (
 job_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind TEXT NOT NULL,
 scope TEXT NOT NULL REFERENCES conversations(scope), status TEXT NOT NULL,
 due_at TIMESTAMPTZ NOT NULL, first_at TIMESTAMPTZ NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, payload JSONB NOT NULL DEFAULT '{}', error TEXT
);
CREATE UNIQUE INDEX pending_job ON jobs(kind, scope) WHERE status = 'pending';
CREATE INDEX due_jobs ON jobs(due_at, job_id) WHERE status = 'pending';
CREATE INDEX jobs_scope ON jobs(scope);
CREATE TABLE outbox (
 send_id TEXT PRIMARY KEY, cursor BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 scope TEXT NOT NULL REFERENCES conversations(scope),
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id), source_revision BIGINT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL, not_before TIMESTAMPTZ NOT NULL, sent_at TIMESTAMPTZ,
 external_message_id TEXT, error TEXT
);
CREATE INDEX outbox_scope ON outbox(scope, cursor);
CREATE INDEX pending_outbox ON outbox(not_before) WHERE status = 'pending';
CREATE TABLE memories (
 memory_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 scope TEXT NOT NULL REFERENCES conversations(scope),
 kind TEXT NOT NULL CHECK (kind IN ('statement', 'summary', 'inference')),
 content TEXT NOT NULL, source_event_ids BIGINT[] NOT NULL CHECK (cardinality(source_event_ids) > 0),
 source_version BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX memories_scope ON memories(scope, memory_id);
CREATE TABLE audit_log (
 audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 scope TEXT NOT NULL, action TEXT NOT NULL, details JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX audit_scope ON audit_log(scope, audit_id);
CREATE TABLE mock_receipts (
 send_id TEXT PRIMARY KEY, scope TEXT NOT NULL, text TEXT NOT NULL,
 external_message_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
);
INSERT INTO schema_version(version) VALUES (1);
