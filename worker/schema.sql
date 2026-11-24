CREATE TABLE IF NOT EXISTS decisions (
  path       TEXT PRIMARY KEY,          -- photo path relative to the share root
  folder     TEXT NOT NULL,             -- containing folder ('' = root)
  decision   TEXT NOT NULL CHECK (decision IN ('keep', 'delete')),
  trashed    TEXT,                      -- location inside _trash, for undo
  decided_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_folder ON decisions (folder);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
