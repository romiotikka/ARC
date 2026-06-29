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
    team_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    country TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS team_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL,
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
    player_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    birth_date TEXT,
    nationality TEXT,
    height_cm INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS player_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    UNIQUE (player_id, alias_name)
);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season_id INTEGER NOT NULL,
    game_date TEXT,
    home_team_id TEXT,
    away_team_id TEXT,
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
    team_id TEXT NOT NULL,
    opponent_team_id TEXT,
    is_home INTEGER,
    points INTEGER,
    provider_team_id TEXT, -- Original ID from the data provider
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (opponent_team_id) REFERENCES teams(team_id),
    UNIQUE (game_id, team_id)
);

CREATE TABLE IF NOT EXISTS player_games (
    player_game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    player_id TEXT, -- ARC persistent player identity
    team_id TEXT,   -- ARC persistent team identity
    provider_player_id TEXT, -- Original player ID from provider
    provider_team_id TEXT,   -- Original team ID from provider
    player_name TEXT NOT NULL,
    team_name TEXT NOT NULL,
    shirt_number TEXT,
    position TEXT,
    minutes TEXT,
    points INTEGER,
    off_reb INTEGER,
    def_reb INTEGER,
    tot_reb INTEGER,
    assists INTEGER,
    steals INTEGER,
    blocks INTEGER,
    turnovers INTEGER,
    fgm INTEGER,
    fga INTEGER,
    tpm INTEGER,
    tpa INTEGER,
    ftm INTEGER,
    fta INTEGER,
    plus_minus INTEGER,
    starter INTEGER,
    source TEXT NOT NULL DEFAULT 'fiba_livestats',
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
    team_id TEXT,
    player_id TEXT,
    event_secondary_player_id TEXT,
    event_type TEXT,
    event_result TEXT,
    shot_type TEXT,
    shot_move TEXT,
    points INTEGER,
    x REAL,
    y REAL,
    possession_team_id_after TEXT,
    shot_clock REAL,
    contested INTEGER,
    distance REAL,
    metadata_json TEXT,
    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (team_id) REFERENCES teams(team_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    FOREIGN KEY (event_secondary_player_id) REFERENCES players(player_id),
    FOREIGN KEY (possession_team_id_after) REFERENCES teams(team_id),
    UNIQUE (game_id, action_number)
);

CREATE TABLE IF NOT EXISTS lineups (
    lineup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL,
    player_1 TEXT NOT NULL,
    player_2 TEXT NOT NULL,
    player_3 TEXT NOT NULL,
    player_4 TEXT NOT NULL,
    player_5 TEXT NOT NULL,
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
    team_id TEXT NOT NULL,
    period INTEGER,
    start_action_number INTEGER,
    end_action_number INTEGER,
    start_clock TEXT,
    end_clock TEXT,
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

CREATE INDEX IF NOT EXISTS idx_games_league_season
    ON games (league_id, season_id);

CREATE INDEX IF NOT EXISTS idx_player_games_game
    ON player_games (game_id);

CREATE INDEX IF NOT EXISTS idx_player_games_player
    ON player_games (player_id);

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

