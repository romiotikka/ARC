-- External ID mapping tables for team and season identifiers.
-- Run once against an existing ARC database to enable provider roster resolution.

BEGIN;

CREATE TABLE IF NOT EXISTS team_external_ids (
    team_id          TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    external_team_id TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    PRIMARY KEY (team_id, provider),
    UNIQUE (provider, external_team_id)
);

CREATE TABLE IF NOT EXISTS season_external_ids (
    season_id          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    external_season_id TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    PRIMARY KEY (season_id, provider),
    UNIQUE (provider, external_season_id)
);

CREATE INDEX IF NOT EXISTS idx_team_external_ids_provider
    ON team_external_ids (provider, team_id);

CREATE INDEX IF NOT EXISTS idx_season_external_ids_provider
    ON season_external_ids (provider, season_id);

COMMIT;
