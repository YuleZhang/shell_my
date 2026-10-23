CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, report_id TEXT,
 idem TEXT NOT NULL UNIQUE, input TEXT NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created_at);
CREATE TABLE IF NOT EXISTS report_versions (
 report_id TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(report_id, revision)
);
