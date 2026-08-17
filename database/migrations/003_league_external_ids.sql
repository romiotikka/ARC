-- Provider-specific mapping for ARC league identities.
-- External values remain TEXT because provider identifiers are not constrained
-- to ARC's internal integer representation.

BEGIN;

CREATE TABLE IF NOT EXISTS league_external_ids (
    league_id          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    external_league_id TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    PRIMARY KEY (league_id, provider),
    UNIQUE (provider, external_league_id)
);

CREATE INDEX IF NOT EXISTS idx_league_external_ids_provider
    ON league_external_ids (provider, league_id);

COMMIT;
