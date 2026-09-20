-- Dead-letter records for failed source imports.

BEGIN;

CREATE TABLE IF NOT EXISTS failed_imports (
    failed_import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    league_id INTEGER NOT NULL,
    season_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    match_id TEXT NOT NULL,
    provider_game_id TEXT,
    entity_type TEXT,
    entity_key TEXT,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    context_json TEXT,
    failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error_at TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    UNIQUE (game_id, match_id)
);

CREATE INDEX IF NOT EXISTS idx_failed_imports_status
    ON failed_imports (status);

CREATE INDEX IF NOT EXISTS idx_failed_imports_failed_at
    ON failed_imports (failed_at);

CREATE INDEX IF NOT EXISTS idx_failed_imports_retry_count
    ON failed_imports (retry_count);

COMMIT;