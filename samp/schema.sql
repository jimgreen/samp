CREATE TABLE IF NOT EXISTS samp_define (
    id INTEGER PRIMARY KEY,
    samp_name TEXT NOT NULL UNIQUE,
    source_database TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL,
    target_dir TEXT NOT NULL DEFAULT '',
    target_name TEXT NOT NULL DEFAULT '',
    field_name TEXT NOT NULL,
    items_json TEXT NOT NULL DEFAULT '',
    samp_type INTEGER NOT NULL DEFAULT 1,
    samp_time INTEGER NOT NULL DEFAULT 60 CHECK (samp_time >= 0),
    filter_value TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    trigger_time INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

