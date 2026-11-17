PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS leagues (
    league_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT,
    region TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS seasons (
    season_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    start_year INTEGER NOT NULL,
    end_year INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    country TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS team_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    alias_name TEXT NOT NULL,
    valid_from_season_id INTEGER,
    valid_to_season_id INTEGER,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (valid_from_season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (valid_to_season_id) REFERENCES seasons(season_id),
    UNIQUE (team_id, alias_name)
);

CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT,
    last_name TEXT,
    canonical_name TEXT NOT NULL,
    birth_date TEXT,
    nationality TEXT,
    height_cm INTEGER,
    position TEXT CHECK (position IS NULL OR position IN ('G', 'F', 'C', 'G-F', 'F-C')),
    identity_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (identity_status IN ('unverified', 'conflicted', 'verified')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS player_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL,
    alias_name TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    UNIQUE (player_id, alias_name)
);

CREATE TABLE IF NOT EXISTS player_external_ids (
    player_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    external_player_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    PRIMARY KEY (player_id, provider, external_player_id),
    UNIQUE (provider, external_player_id)
);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season_id INTEGER NOT NULL,
    game_date TEXT,
    home_team_id INTEGER,
    away_team_id INTEGER,
    venue TEXT,
    attendance INTEGER,
    home_score INTEGER,
    away_score INTEGER,
    status TEXT,
    competition_type TEXT, -- e.g., Regular Season, Playoffs, Cup
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
    FOREIGN KEY (away_team_id) REFERENCES teams(team_id)
);

CREATE TABLE IF NOT EXISTS team_games (
    team_game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    opponent_team_id INTEGER,
    is_home INTEGER,
    points INTEGER,
    period_1_points INTEGER,
    period_2_points INTEGER,
    period_3_points INTEGER,
    period_4_points INTEGER,
    overtime_points INTEGER,
    field_goals_made INTEGER,
    field_goals_attempted INTEGER,
    three_pointers_made INTEGER,
    three_pointers_attempted INTEGER,
    free_throws_made INTEGER,
    free_throws_attempted INTEGER,
    off_reb INTEGER,
    def_reb INTEGER,
    tot_reb INTEGER,
    team_rebounds INTEGER,
    team_off_reb INTEGER,
    team_def_reb INTEGER,
    assists INTEGER,
    steals INTEGER,
    blocks INTEGER,
    blocks_received INTEGER,
    turnovers INTEGER,
    team_turnovers INTEGER,
    personal_fouls INTEGER,
    fouls_drawn INTEGER,
    team_fouls INTEGER,
    total_fouls INTEGER,
    points_fast_break INTEGER,
    points_from_turnovers INTEGER,
    points_in_paint INTEGER,
    points_second_chance INTEGER,
    bench_points INTEGER,
    biggest_lead INTEGER,
    biggest_scoring_run INTEGER,
    lead_changes INTEGER,
    times_scores_level INTEGER,
    provider_team_id TEXT, -- Original ID from the data provider
    source_team_occurrence_id INTEGER REFERENCES source_team_occurrences(team_occurrence_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (opponent_team_id) REFERENCES teams(team_id),
    UNIQUE (game_id, team_id)
);

CREATE TABLE IF NOT EXISTS player_games (
    player_game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    player_id INTEGER, -- ARC persistent player identity
    team_id INTEGER,   -- ARC persistent team identity
    provider_player_id TEXT, -- Original player ID from provider
    provider_team_id TEXT,   -- Original team ID from provider
    player_name TEXT NOT NULL,
    team_name TEXT NOT NULL,
    shirt_number TEXT,
    position TEXT,
    minutes TEXT,
    points INTEGER,
    points_fast_break INTEGER,
    points_in_paint INTEGER,
    points_second_chance INTEGER,
    off_reb INTEGER,
    def_reb INTEGER,
    tot_reb INTEGER,
    assists INTEGER,
    steals INTEGER,
    blocks INTEGER,
    blocks_received INTEGER,
    personal_fouls INTEGER,
    fouls_drawn INTEGER,
    turnovers INTEGER,
    fgm INTEGER,
    fga INTEGER,
    tpm INTEGER,
    tpa INTEGER,
    ftm INTEGER,
    fta INTEGER,
    plus_minus INTEGER,
    starter INTEGER,
    captain INTEGER CHECK (captain IS NULL OR captain IN (0, 1)),
    source TEXT NOT NULL DEFAULT 'fiba_livestats',
    source_player_occurrence_id INTEGER REFERENCES source_player_occurrences(player_occurrence_id),
    identity_decision_id INTEGER REFERENCES identity_decisions(identity_decision_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    UNIQUE (game_id, player_name, team_name, shirt_number)
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    action_number INTEGER,
    period INTEGER,
    clock TEXT,
    team_id INTEGER,
    player_id INTEGER,
    event_secondary_player_id INTEGER,
    event_type TEXT,
    event_result TEXT,
    shot_type TEXT,
    shot_move TEXT,
    points INTEGER,
    x REAL,
    y REAL,
    coordinate_system TEXT,
    possession_team_id_after INTEGER,
    shot_clock REAL,
    contested INTEGER,
    distance REAL,
    metadata_json TEXT,
    source_event_occurrence_id INTEGER REFERENCES source_event_occurrences(event_occurrence_id),
    primary_source_player_occurrence_id INTEGER REFERENCES source_player_occurrences(player_occurrence_id),
    secondary_source_player_occurrence_id INTEGER REFERENCES source_player_occurrences(player_occurrence_id),
    primary_identity_decision_id INTEGER REFERENCES identity_decisions(identity_decision_id),
    secondary_identity_decision_id INTEGER REFERENCES identity_decisions(identity_decision_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (event_secondary_player_id) REFERENCES players(player_id),
    FOREIGN KEY (possession_team_id_after) REFERENCES teams(team_id),
    UNIQUE (game_id, action_number)
);

CREATE TABLE IF NOT EXISTS lineups (
    lineup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    player_1 INTEGER NOT NULL,
    player_2 INTEGER NOT NULL,
    player_3 INTEGER NOT NULL,
    player_4 INTEGER NOT NULL,
    player_5 INTEGER NOT NULL,
    lineup_hash TEXT NOT NULL,
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (player_1) REFERENCES players(player_id),
    FOREIGN KEY (player_2) REFERENCES players(player_id),
    FOREIGN KEY (player_3) REFERENCES players(player_id),
    FOREIGN KEY (player_4) REFERENCES players(player_id),
    FOREIGN KEY (player_5) REFERENCES players(player_id),
    UNIQUE (team_id, lineup_hash)
);

CREATE TABLE IF NOT EXISTS lineup_segments (
    lineup_segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    lineup_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    period INTEGER,
    start_action_number INTEGER,
    end_action_number INTEGER,
    start_clock TEXT,
    end_clock TEXT,
    duration_seconds INTEGER,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (lineup_id) REFERENCES lineups(lineup_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id)
);

CREATE TABLE IF NOT EXISTS source_livestats_games (
    game_id TEXT PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season_id INTEGER NOT NULL,
    provider_game_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    json_url TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    FOREIGN KEY (season_id) REFERENCES seasons(season_id),
    UNIQUE (match_id)
);

-- Immutable provider captures and their versioned ARC interpretations.
CREATE TABLE IF NOT EXISTS source_import_runs (
    import_run_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    capture_kind TEXT NOT NULL CHECK (capture_kind IN ('initial_import', 'reused_snapshot', 'explicit_recapture', 'historical_recapture')),
    status TEXT NOT NULL CHECK (status IN ('capturing', 'captured', 'imported', 'failed')),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    importer_version TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS source_game_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_game_id TEXT,
    source_url TEXT,
    capture_kind TEXT NOT NULL CHECK (capture_kind IN ('initial_import', 'explicit_recapture', 'historical_recapture')),
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    content_type TEXT,
    content_encoding TEXT,
    payload_gzip BLOB NOT NULL,
    captured_body_sha256 TEXT NOT NULL CHECK (length(captured_body_sha256) = 64),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    UNIQUE (game_id, provider, captured_body_sha256),
    UNIQUE (snapshot_id, game_id)
);

CREATE TABLE IF NOT EXISTS source_import_run_snapshots (
    import_run_id TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    PRIMARY KEY (import_run_id, snapshot_id),
    FOREIGN KEY (import_run_id) REFERENCES source_import_runs(import_run_id),
    FOREIGN KEY (snapshot_id) REFERENCES source_game_snapshots(snapshot_id)
);

-- A single serialized per-import projection keeps candidates separate from
-- the existing analytical tables until reconstruction and promotion commit.
CREATE TABLE IF NOT EXISTS source_import_analytical_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_run_id TEXT NOT NULL UNIQUE,
    game_id TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    normalization_id INTEGER NOT NULL,
    decision_set_id INTEGER,
    status TEXT NOT NULL CHECK (status IN (
        'candidate_importing', 'ready_for_reconstruction', 'ready_for_promotion', 'review_required',
        'reconstruction_failed', 'promotion_failed', 'promoted', 'import_failed'
    )),
    candidate_json TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (import_run_id, snapshot_id)
        REFERENCES source_import_run_snapshots(import_run_id, snapshot_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (snapshot_id, game_id)
        REFERENCES source_game_snapshots(snapshot_id, game_id),
    FOREIGN KEY (normalization_id, snapshot_id, game_id)
        REFERENCES source_snapshot_normalizations(normalization_id, snapshot_id, game_id),
    FOREIGN KEY (decision_set_id, normalization_id)
        REFERENCES identity_decision_sets(decision_set_id, normalization_id)
);

CREATE INDEX IF NOT EXISTS idx_source_candidates_game_status
    ON source_import_analytical_candidates(game_id, status, candidate_id);

CREATE TRIGGER IF NOT EXISTS trg_source_candidates_payload_immutable
BEFORE UPDATE OF import_run_id, game_id, snapshot_id, normalization_id, candidate_json
ON source_import_analytical_candidates BEGIN
    SELECT RAISE(ABORT, 'candidate payload and source links are immutable');
END;

CREATE TABLE IF NOT EXISTS source_snapshot_normalizations (
    normalization_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    game_id TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    normalized_json_gzip BLOB NOT NULL,
    normalized_sha256 TEXT NOT NULL CHECK (length(normalized_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial_backfill')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (snapshot_id, game_id) REFERENCES source_game_snapshots(snapshot_id, game_id),
    UNIQUE (snapshot_id, normalizer_version),
    UNIQUE (normalization_id, snapshot_id),
    UNIQUE (normalization_id, game_id),
    UNIQUE (normalization_id, snapshot_id, game_id)
);

CREATE TABLE IF NOT EXISTS source_team_occurrences (
    team_occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalization_id INTEGER NOT NULL,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    source_team_key TEXT,
    provider_team_id TEXT,
    team_name TEXT,
    home_away TEXT CHECK (home_away IS NULL OR home_away IN ('home', 'away')),
    FOREIGN KEY (normalization_id) REFERENCES source_snapshot_normalizations(normalization_id),
    UNIQUE (normalization_id, source_ordinal),
    UNIQUE (team_occurrence_id, normalization_id)
);

CREATE TABLE IF NOT EXISTS source_player_occurrences (
    player_occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalization_id INTEGER NOT NULL,
    team_occurrence_id INTEGER NOT NULL,
    identity_occurrence_key TEXT NOT NULL,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    roster_entry_key TEXT,
    provider_player_id TEXT,
    pno TEXT,
    original_name TEXT,
    display_name TEXT,
    first_name TEXT,
    last_name TEXT,
    jersey_number TEXT,
    position TEXT,
    starter INTEGER CHECK (starter IS NULL OR starter IN (0, 1)),
    FOREIGN KEY (team_occurrence_id, normalization_id)
        REFERENCES source_team_occurrences(team_occurrence_id, normalization_id),
    UNIQUE (team_occurrence_id, source_ordinal),
    UNIQUE (normalization_id, identity_occurrence_key),
    UNIQUE (player_occurrence_id, normalization_id)
);

CREATE TABLE IF NOT EXISTS source_event_occurrences (
    event_occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalization_id INTEGER NOT NULL,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    source_event_id TEXT,
    provider_action_number INTEGER,
    period INTEGER,
    clock TEXT,
    team_occurrence_id INTEGER,
    action_type TEXT,
    subtype TEXT,
    success INTEGER CHECK (success IS NULL OR success IN (0, 1)),
    primary_player_occurrence_id INTEGER,
    secondary_player_occurrence_id INTEGER,
    primary_original_name TEXT,
    primary_provider_player_id TEXT,
    primary_pno TEXT,
    primary_jersey_number TEXT,
    secondary_original_name TEXT,
    secondary_provider_player_id TEXT,
    secondary_pno TEXT,
    secondary_jersey_number TEXT,
    primary_identity_status TEXT CHECK (primary_identity_status IS NULL OR primary_identity_status IN ('resolved', 'ambiguous', 'conflict', 'unresolved', 'review', 'not_applicable')),
    secondary_identity_status TEXT CHECK (secondary_identity_status IS NULL OR secondary_identity_status IN ('resolved', 'ambiguous', 'conflict', 'unresolved', 'review', 'not_applicable')),
    previous_action_number TEXT,
    provider_shot_previous_action TEXT,
    provider_shot_result INTEGER CHECK (provider_shot_result IS NULL OR provider_shot_result IN (0, 1)),
    provider_shot_x REAL,
    provider_shot_y REAL,
    provider_shot_player_ref TEXT,
    FOREIGN KEY (normalization_id) REFERENCES source_snapshot_normalizations(normalization_id),
    FOREIGN KEY (team_occurrence_id, normalization_id)
        REFERENCES source_team_occurrences(team_occurrence_id, normalization_id),
    FOREIGN KEY (primary_player_occurrence_id, normalization_id)
        REFERENCES source_player_occurrences(player_occurrence_id, normalization_id),
    FOREIGN KEY (secondary_player_occurrence_id, normalization_id)
        REFERENCES source_player_occurrences(player_occurrence_id, normalization_id),
    UNIQUE (normalization_id, source_ordinal),
    UNIQUE (event_occurrence_id, normalization_id)
);

-- Provider event links are directed from the action carrying the link to the
-- referenced source action. Keeping a relation table allows multiple links per
-- event and retains the provider field/value even when its target is missing.
CREATE TABLE IF NOT EXISTS event_relationships (
    event_relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    from_event_id INTEGER NOT NULL,
    to_event_id INTEGER,
    from_source_event_occurrence_id INTEGER NOT NULL,
    to_source_event_occurrence_id INTEGER,
    relationship_type TEXT NOT NULL,
    source_field TEXT NOT NULL,
    source_value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (from_event_id) REFERENCES events(event_id),
    FOREIGN KEY (to_event_id) REFERENCES events(event_id),
    FOREIGN KEY (from_source_event_occurrence_id) REFERENCES source_event_occurrences(event_occurrence_id),
    FOREIGN KEY (to_source_event_occurrence_id) REFERENCES source_event_occurrences(event_occurrence_id),
    UNIQUE (game_id, from_event_id, source_field, source_value)
);
CREATE INDEX IF NOT EXISTS idx_event_relationships_to_event
    ON event_relationships(game_id, to_event_id);

CREATE TABLE IF NOT EXISTS identity_decision_sets (
    decision_set_id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalization_id INTEGER NOT NULL,
    decision_set_version INTEGER NOT NULL CHECK (decision_set_version > 0),
    resolver_version TEXT NOT NULL,
    decision_set_sha256 TEXT NOT NULL CHECK (length(decision_set_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('complete', 'contains_review', 'incomplete')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (normalization_id) REFERENCES source_snapshot_normalizations(normalization_id),
    UNIQUE (normalization_id, decision_set_version),
    UNIQUE (decision_set_id, normalization_id)
);

CREATE TABLE IF NOT EXISTS identity_decisions (
    identity_decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_set_id INTEGER NOT NULL,
    normalization_id INTEGER NOT NULL,
    player_occurrence_id INTEGER NOT NULL,
    arc_player_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'conflict', 'unresolved', 'review')),
    decision_source TEXT NOT NULL CHECK (decision_source IN ('resolver', 'manual')),
    evidence_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (decision_set_id, normalization_id)
        REFERENCES identity_decision_sets(decision_set_id, normalization_id),
    FOREIGN KEY (player_occurrence_id, normalization_id)
        REFERENCES source_player_occurrences(player_occurrence_id, normalization_id),
    FOREIGN KEY (arc_player_id) REFERENCES players(player_id),
    UNIQUE (decision_set_id, player_occurrence_id),
    UNIQUE (identity_decision_id, normalization_id),
    CHECK ((status = 'resolved' AND arc_player_id IS NOT NULL)
        OR (status <> 'resolved' AND arc_player_id IS NULL))
);

CREATE TABLE IF NOT EXISTS active_game_source_snapshots (
    game_id TEXT PRIMARY KEY,
    snapshot_id INTEGER NOT NULL UNIQUE,
    normalization_id INTEGER NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (snapshot_id, game_id) REFERENCES source_game_snapshots(snapshot_id, game_id),
    FOREIGN KEY (normalization_id, snapshot_id, game_id)
        REFERENCES source_snapshot_normalizations(normalization_id, snapshot_id, game_id),
    UNIQUE (game_id, normalization_id)
);

CREATE TABLE IF NOT EXISTS active_identity_decision_sets (
    game_id TEXT PRIMARY KEY,
    normalization_id INTEGER NOT NULL,
    decision_set_id INTEGER NOT NULL,
    FOREIGN KEY (game_id, normalization_id)
        REFERENCES active_game_source_snapshots(game_id, normalization_id),
    FOREIGN KEY (decision_set_id, normalization_id)
        REFERENCES identity_decision_sets(decision_set_id, normalization_id)
);

CREATE TABLE IF NOT EXISTS reconstruction_runs (
    reconstruction_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    normalization_id INTEGER NOT NULL,
    decision_set_id INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    output_sha256 TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    error_message TEXT,
    FOREIGN KEY (normalization_id, snapshot_id, game_id)
        REFERENCES source_snapshot_normalizations(normalization_id, snapshot_id, game_id),
    FOREIGN KEY (decision_set_id, normalization_id)
        REFERENCES identity_decision_sets(decision_set_id, normalization_id),
    UNIQUE (reconstruction_run_id, game_id)
);

CREATE TABLE IF NOT EXISTS reconstruction_run_segments (
    reconstruction_run_segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reconstruction_run_id INTEGER NOT NULL,
    game_id TEXT NOT NULL,
    lineup_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    period INTEGER,
    start_action_number INTEGER,
    end_action_number INTEGER,
    start_clock TEXT,
    end_clock TEXT,
    duration_seconds INTEGER,
    FOREIGN KEY (reconstruction_run_id, game_id)
        REFERENCES reconstruction_runs(reconstruction_run_id, game_id),
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (lineup_id) REFERENCES lineups(lineup_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id)
);

CREATE TABLE IF NOT EXISTS active_reconstructions (
    game_id TEXT PRIMARY KEY,
    reconstruction_run_id INTEGER NOT NULL UNIQUE,
    activated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (reconstruction_run_id, game_id)
        REFERENCES reconstruction_runs(reconstruction_run_id, game_id)
);

CREATE TRIGGER IF NOT EXISTS trg_source_game_snapshots_no_update
BEFORE UPDATE ON source_game_snapshots BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_game_snapshots_no_delete
BEFORE DELETE ON source_game_snapshots BEGIN
    SELECT RAISE(ABORT, 'source snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_normalizations_no_update
BEFORE UPDATE ON source_snapshot_normalizations BEGIN
    SELECT RAISE(ABORT, 'source normalizations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_normalizations_no_delete
BEFORE DELETE ON source_snapshot_normalizations BEGIN
    SELECT RAISE(ABORT, 'source normalizations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_team_occurrences_no_update
BEFORE UPDATE ON source_team_occurrences BEGIN
    SELECT RAISE(ABORT, 'source occurrences are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_team_occurrences_no_delete
BEFORE DELETE ON source_team_occurrences BEGIN
    SELECT RAISE(ABORT, 'source occurrences are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_player_occurrences_no_update
BEFORE UPDATE ON source_player_occurrences BEGIN
    SELECT RAISE(ABORT, 'source occurrences are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_player_occurrences_no_delete
BEFORE DELETE ON source_player_occurrences BEGIN
    SELECT RAISE(ABORT, 'source occurrences are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_event_occurrences_no_update
BEFORE UPDATE ON source_event_occurrences BEGIN
    SELECT RAISE(ABORT, 'source occurrences are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_event_occurrences_no_delete
BEFORE DELETE ON source_event_occurrences BEGIN
    SELECT RAISE(ABORT, 'source occurrences are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_identity_decision_sets_no_update
BEFORE UPDATE ON identity_decision_sets BEGIN
    SELECT RAISE(ABORT, 'identity decision sets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_identity_decision_sets_no_delete
BEFORE DELETE ON identity_decision_sets BEGIN
    SELECT RAISE(ABORT, 'identity decision sets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_identity_decisions_no_update
BEFORE UPDATE ON identity_decisions BEGIN
    SELECT RAISE(ABORT, 'identity decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_identity_decisions_no_delete
BEFORE DELETE ON identity_decisions BEGIN
    SELECT RAISE(ABORT, 'identity decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_run_segments_no_update
BEFORE UPDATE ON reconstruction_run_segments BEGIN
    SELECT RAISE(ABORT, 'reconstruction run outputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_run_segments_no_delete
BEFORE DELETE ON reconstruction_run_segments BEGIN
    SELECT RAISE(ABORT, 'reconstruction run outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_team_games_source_game_insert
BEFORE INSERT ON team_games
WHEN NEW.source_team_occurrence_id IS NOT NULL AND
     (SELECT n.game_id FROM source_team_occurrences o
      JOIN source_snapshot_normalizations n USING (normalization_id)
      WHERE o.team_occurrence_id = NEW.source_team_occurrence_id) IS NOT NEW.game_id
BEGIN
    SELECT RAISE(ABORT, 'team source occurrence belongs to another game');
END;
CREATE TRIGGER IF NOT EXISTS trg_team_games_source_game_update
BEFORE UPDATE OF source_team_occurrence_id, game_id ON team_games
WHEN NEW.source_team_occurrence_id IS NOT NULL AND
     (SELECT n.game_id FROM source_team_occurrences o
      JOIN source_snapshot_normalizations n USING (normalization_id)
      WHERE o.team_occurrence_id = NEW.source_team_occurrence_id) IS NOT NEW.game_id
BEGIN
    SELECT RAISE(ABORT, 'team source occurrence belongs to another game');
END;

CREATE TRIGGER IF NOT EXISTS trg_player_games_source_identity_insert
BEFORE INSERT ON player_games
WHEN (NEW.source_player_occurrence_id IS NOT NULL AND
      (SELECT n.game_id FROM source_player_occurrences o
       JOIN source_snapshot_normalizations n USING (normalization_id)
       WHERE o.player_occurrence_id = NEW.source_player_occurrence_id) IS NOT NEW.game_id)
  OR (NEW.identity_decision_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM identity_decisions d
      JOIN source_player_occurrences o ON o.player_occurrence_id = d.player_occurrence_id
      JOIN source_snapshot_normalizations n ON n.normalization_id = o.normalization_id
      WHERE d.identity_decision_id = NEW.identity_decision_id
        AND d.player_occurrence_id = NEW.source_player_occurrence_id
        AND n.game_id = NEW.game_id))
BEGIN
    SELECT RAISE(ABORT, 'player-game source identity belongs to another occurrence or game');
END;
CREATE TRIGGER IF NOT EXISTS trg_player_games_source_identity_update
BEFORE UPDATE OF source_player_occurrence_id, identity_decision_id, game_id ON player_games
WHEN (NEW.source_player_occurrence_id IS NOT NULL AND
      (SELECT n.game_id FROM source_player_occurrences o
       JOIN source_snapshot_normalizations n USING (normalization_id)
       WHERE o.player_occurrence_id = NEW.source_player_occurrence_id) IS NOT NEW.game_id)
  OR (NEW.identity_decision_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM identity_decisions d
      JOIN source_player_occurrences o ON o.player_occurrence_id = d.player_occurrence_id
      JOIN source_snapshot_normalizations n ON n.normalization_id = o.normalization_id
      WHERE d.identity_decision_id = NEW.identity_decision_id
        AND d.player_occurrence_id = NEW.source_player_occurrence_id
        AND n.game_id = NEW.game_id))
BEGIN
    SELECT RAISE(ABORT, 'player-game source identity belongs to another occurrence or game');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_source_identity_insert
BEFORE INSERT ON events
WHEN (NEW.source_event_occurrence_id IS NOT NULL AND
      (SELECT n.game_id FROM source_event_occurrences e
       JOIN source_snapshot_normalizations n USING (normalization_id)
       WHERE e.event_occurrence_id = NEW.source_event_occurrence_id) IS NOT NEW.game_id)
  OR (NEW.primary_source_player_occurrence_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM source_event_occurrences e
      JOIN source_snapshot_normalizations n ON n.normalization_id = e.normalization_id
      WHERE e.event_occurrence_id = NEW.source_event_occurrence_id
        AND e.primary_player_occurrence_id = NEW.primary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
  OR (NEW.secondary_source_player_occurrence_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM source_event_occurrences e
      JOIN source_snapshot_normalizations n ON n.normalization_id = e.normalization_id
      WHERE e.event_occurrence_id = NEW.source_event_occurrence_id
        AND e.secondary_player_occurrence_id = NEW.secondary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
  OR (NEW.primary_identity_decision_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM identity_decisions d JOIN source_snapshot_normalizations n USING (normalization_id)
      WHERE d.identity_decision_id = NEW.primary_identity_decision_id
        AND d.player_occurrence_id = NEW.primary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
  OR (NEW.secondary_identity_decision_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM identity_decisions d JOIN source_snapshot_normalizations n USING (normalization_id)
      WHERE d.identity_decision_id = NEW.secondary_identity_decision_id
        AND d.player_occurrence_id = NEW.secondary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
BEGIN
    SELECT RAISE(ABORT, 'event source identity belongs to another occurrence or game');
END;
CREATE TRIGGER IF NOT EXISTS trg_events_source_identity_update
BEFORE UPDATE OF source_event_occurrence_id, primary_source_player_occurrence_id,
                 secondary_source_player_occurrence_id, primary_identity_decision_id,
                 secondary_identity_decision_id, game_id ON events
WHEN (NEW.source_event_occurrence_id IS NOT NULL AND
      (SELECT n.game_id FROM source_event_occurrences e
       JOIN source_snapshot_normalizations n USING (normalization_id)
       WHERE e.event_occurrence_id = NEW.source_event_occurrence_id) IS NOT NEW.game_id)
  OR (NEW.primary_source_player_occurrence_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM source_event_occurrences e
      JOIN source_snapshot_normalizations n ON n.normalization_id = e.normalization_id
      WHERE e.event_occurrence_id = NEW.source_event_occurrence_id
        AND e.primary_player_occurrence_id = NEW.primary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
  OR (NEW.secondary_source_player_occurrence_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM source_event_occurrences e
      JOIN source_snapshot_normalizations n ON n.normalization_id = e.normalization_id
      WHERE e.event_occurrence_id = NEW.source_event_occurrence_id
        AND e.secondary_player_occurrence_id = NEW.secondary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
  OR (NEW.primary_identity_decision_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM identity_decisions d JOIN source_snapshot_normalizations n USING (normalization_id)
      WHERE d.identity_decision_id = NEW.primary_identity_decision_id
        AND d.player_occurrence_id = NEW.primary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
  OR (NEW.secondary_identity_decision_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM identity_decisions d JOIN source_snapshot_normalizations n USING (normalization_id)
      WHERE d.identity_decision_id = NEW.secondary_identity_decision_id
        AND d.player_occurrence_id = NEW.secondary_source_player_occurrence_id
        AND n.game_id = NEW.game_id))
BEGIN
    SELECT RAISE(ABORT, 'event source identity belongs to another occurrence or game');
END;


CREATE INDEX IF NOT EXISTS idx_games_league_season
    ON games (league_id, season_id);

CREATE INDEX IF NOT EXISTS idx_player_games_game
    ON player_games (game_id);

CREATE INDEX IF NOT EXISTS idx_player_games_player
    ON player_games (player_id);

CREATE INDEX IF NOT EXISTS idx_source_snapshots_game_provider
    ON source_game_snapshots (game_id, provider, captured_at);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_provider_game
    ON source_game_snapshots (provider, provider_game_id);
CREATE INDEX IF NOT EXISTS idx_source_normalizations_snapshot
    ON source_snapshot_normalizations (snapshot_id, status);
CREATE INDEX IF NOT EXISTS idx_source_team_occurrences_norm
    ON source_team_occurrences (normalization_id, source_ordinal);
CREATE INDEX IF NOT EXISTS idx_source_player_occurrences_team
    ON source_player_occurrences (team_occurrence_id, source_ordinal);
CREATE INDEX IF NOT EXISTS idx_source_event_occurrences_order
    ON source_event_occurrences (normalization_id, source_ordinal);
CREATE INDEX IF NOT EXISTS idx_identity_decisions_occurrence
    ON identity_decisions (player_occurrence_id, decision_set_id);
CREATE INDEX IF NOT EXISTS idx_reconstruction_runs_game
    ON reconstruction_runs (game_id, started_at);
CREATE INDEX IF NOT EXISTS idx_reconstruction_run_segments_order
    ON reconstruction_run_segments (reconstruction_run_id, team_id, period, start_action_number);

CREATE TABLE IF NOT EXISTS team_external_ids (
    team_id          INTEGER NOT NULL,
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

CREATE TABLE IF NOT EXISTS league_external_ids (
    league_id          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    external_league_id TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    PRIMARY KEY (league_id, provider),
    UNIQUE (provider, external_league_id)
);

CREATE INDEX IF NOT EXISTS idx_team_external_ids_provider
    ON team_external_ids (provider, team_id);

CREATE INDEX IF NOT EXISTS idx_season_external_ids_provider
    ON season_external_ids (provider, season_id);

CREATE INDEX IF NOT EXISTS idx_league_external_ids_provider
    ON league_external_ids (provider, league_id);

CREATE INDEX IF NOT EXISTS idx_players_canonical_name
    ON players (canonical_name);

CREATE INDEX IF NOT EXISTS idx_players_last_first_name
    ON players (last_name, first_name);

CREATE INDEX IF NOT EXISTS idx_player_aliases_alias_name
    ON player_aliases (alias_name);

CREATE INDEX IF NOT EXISTS idx_players_identity_status
    ON players (identity_status);

CREATE INDEX IF NOT EXISTS idx_events_game
    ON events (game_id);

CREATE INDEX IF NOT EXISTS idx_events_action_number
    ON events (game_id, action_number);

CREATE INDEX IF NOT EXISTS idx_events_player
    ON events (game_id, player_id);

CREATE INDEX IF NOT EXISTS idx_events_team
    ON events (game_id, team_id);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON events (game_id, event_type);

CREATE INDEX IF NOT EXISTS idx_lineups_team
    ON lineups (team_id);

CREATE INDEX IF NOT EXISTS idx_lineups_hash
    ON lineups (team_id, lineup_hash);

CREATE INDEX IF NOT EXISTS idx_lineup_segments_game_team
    ON lineup_segments (game_id, team_id);

CREATE INDEX IF NOT EXISTS idx_lineup_segments_game_lineup
    ON lineup_segments (game_id, lineup_id);

CREATE INDEX IF NOT EXISTS idx_lineup_segments_lineup
    ON lineup_segments (lineup_id);

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
