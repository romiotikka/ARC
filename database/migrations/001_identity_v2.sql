-- Identity v2 schema upgrade.
-- Run once against an existing ARC database before enabling the Identity Resolver.

BEGIN;

ALTER TABLE players ADD COLUMN first_name TEXT;
ALTER TABLE players ADD COLUMN last_name TEXT;
ALTER TABLE players ADD COLUMN position TEXT
    CHECK (position IS NULL OR position IN ('G', 'F', 'C', 'G-F', 'F-C'));
ALTER TABLE players ADD COLUMN identity_status TEXT NOT NULL DEFAULT 'unverified'
    CHECK (identity_status IN ('unverified', 'conflicted', 'verified'));
ALTER TABLE players ADD COLUMN updated_at TEXT;

-- Existing identities retain their original creation timestamp as their initial update timestamp.
UPDATE players
SET updated_at = created_at
WHERE updated_at IS NULL;

CREATE TABLE IF NOT EXISTS player_external_ids (
    player_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_player_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    PRIMARY KEY (player_id, provider, external_player_id),
    UNIQUE (provider, external_player_id)
);

CREATE INDEX IF NOT EXISTS idx_players_canonical_name
    ON players (canonical_name);

CREATE INDEX IF NOT EXISTS idx_players_last_first_name
    ON players (last_name, first_name);

CREATE INDEX IF NOT EXISTS idx_player_aliases_alias_name
    ON player_aliases (alias_name);

CREATE INDEX IF NOT EXISTS idx_players_identity_status
    ON players (identity_status);

COMMIT;
