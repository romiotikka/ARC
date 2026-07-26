from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATABASE_PATHS = [
    ROOT_DIR / "data" / "arc.db",
    ROOT_DIR / "data" / "arc2.db",
]


REQUIRED_EVENT_COLUMNS = {
    "event_id",
    "game_id",
    "action_number",
    "period",
    "clock",
    "team_id",
    "player_id",
    "event_secondary_player_id",
    "event_type",
    "event_result",
    "shot_type",
    "shot_move",
    "points",
    "x",
    "y",
    "possession_team_id_after",
    "shot_clock",
    "contested",
    "distance",
    "metadata_json",
}


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = OFF;")
    return connection


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def table_has_primary_key(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    for row in rows:
        if row[1] == column_name and row[5] == 1:
            return True
    return False


def has_foreign_key(
    connection: sqlite3.Connection,
    table_name: str,
    from_column: str,
    to_table: str,
    to_column: str,
) -> bool:
    rows = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    for row in rows:
        if row[3] == from_column and row[2] == to_table and row[4] == to_column:
            return True
    return False


def has_unique_index(connection: sqlite3.Connection, table_name: str, columns: tuple[str, ...]) -> bool:
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    for index_row in indexes:
        index_name = index_row[1]
        is_unique = index_row[2]
        if not is_unique:
            continue
        index_columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
        ordered_columns = tuple(row[2] for row in index_columns)
        if ordered_columns == columns:
            return True
    return False


def create_events_table(connection: sqlite3.Connection, table_name: str) -> None:
    connection.execute(
        f"""
        CREATE TABLE {table_name} (
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
        )
        """
    )


def migrate_events_table(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "events"):
        create_events_table(connection, "events")
        return

    existing_columns = table_columns(connection, "events")
    if REQUIRED_EVENT_COLUMNS.issubset(existing_columns):
        return

    create_events_table(connection, "events_new")

    if "provider_action_number" in existing_columns and "action_type" in existing_columns:
        connection.execute(
            """
            INSERT INTO events_new (
                event_id,
                game_id,
                action_number,
                period,
                clock,
                team_id,
                player_id,
                event_type,
                event_result,
                x,
                y,
                metadata_json
            )
            SELECT
                event_id,
                game_id,
                provider_action_number,
                period,
                clock,
                team_id,
                player_id,
                action_type,
                result,
                x,
                y,
                raw_json
            FROM events
            """
        )
    else:
        shared_columns = [column for column in REQUIRED_EVENT_COLUMNS if column in existing_columns]
        if shared_columns:
            columns_sql = ", ".join(shared_columns)
            connection.execute(
                f"""
                INSERT INTO events_new ({columns_sql})
                SELECT {columns_sql}
                FROM events
                """
            )

    connection.execute("DROP TABLE events")
    connection.execute("ALTER TABLE events_new RENAME TO events")


def create_lineups_table(connection: sqlite3.Connection, table_name: str) -> None:
    connection.execute(
        f"""
        CREATE TABLE {table_name} (
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
        )
        """
    )


def create_lineup_segments_table(connection: sqlite3.Connection, table_name: str) -> None:
    connection.execute(
        f"""
        CREATE TABLE {table_name} (
            lineup_segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            lineup_id INTEGER NOT NULL,
            team_id TEXT NOT NULL,
            period INTEGER,
            start_action_number INTEGER,
            end_action_number INTEGER,
            start_clock TEXT,
            end_clock TEXT,
            duration_seconds INTEGER,
            FOREIGN KEY (game_id) REFERENCES games(game_id),
            FOREIGN KEY (lineup_id) REFERENCES lineups(lineup_id),
            FOREIGN KEY (team_id) REFERENCES teams(team_id)
        )
        """
    )


def migrate_lineups_table(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "lineups"):
        create_lineups_table(connection, "lineups")
        return

    has_required = (
        table_has_primary_key(connection, "lineups", "lineup_id")
        and has_foreign_key(connection, "lineups", "team_id", "teams", "team_id")
        and has_unique_index(connection, "lineups", ("team_id", "lineup_hash"))
    )
    if has_required:
        return

    create_lineups_table(connection, "lineups_new")
    connection.execute(
        """
        INSERT INTO lineups_new (
            lineup_id,
            team_id,
            player_1,
            player_2,
            player_3,
            player_4,
            player_5,
            lineup_hash
        )
        SELECT
            lineup_id,
            team_id,
            player_1,
            player_2,
            player_3,
            player_4,
            player_5,
            lineup_hash
        FROM lineups
        """
    )
    connection.execute("DROP TABLE lineups")
    connection.execute("ALTER TABLE lineups_new RENAME TO lineups")


def migrate_lineup_segments_table(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "lineup_segments"):
        create_lineup_segments_table(connection, "lineup_segments")
        return

    existing_columns = table_columns(connection, "lineup_segments")
    has_required = (
        table_has_primary_key(connection, "lineup_segments", "lineup_segment_id")
        and has_foreign_key(connection, "lineup_segments", "game_id", "games", "game_id")
        and has_foreign_key(connection, "lineup_segments", "team_id", "teams", "team_id")
        and has_foreign_key(connection, "lineup_segments", "lineup_id", "lineups", "lineup_id")
        and "duration_seconds" in existing_columns
    )
    if has_required:
        return

    create_lineup_segments_table(connection, "lineup_segments_new")
    duration_seconds_sql = "duration_seconds" if "duration_seconds" in existing_columns else "NULL AS duration_seconds"
    connection.execute(
        """
        INSERT INTO lineup_segments_new (
            lineup_segment_id,
            game_id,
            lineup_id,
            team_id,
            period,
            start_action_number,
            end_action_number,
            start_clock,
            end_clock,
            duration_seconds
        )
        SELECT
            lineup_segment_id,
            game_id,
            lineup_id,
            team_id,
            period,
            start_action_number,
            end_action_number,
            start_clock,
            end_clock,
            {duration_seconds_sql}
        FROM lineup_segments
        """.format(duration_seconds_sql=duration_seconds_sql)
    )
    connection.execute("DROP TABLE lineup_segments")
    connection.execute("ALTER TABLE lineup_segments_new RENAME TO lineup_segments")


def create_lineups_tables(connection: sqlite3.Connection) -> None:
    migrate_lineups_table(connection)
    migrate_lineup_segments_table(connection)


def create_indexes(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_game ON events (game_id)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_action_number ON events (game_id, action_number)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_player ON events (game_id, player_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_team ON events (game_id, team_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events (game_id, event_type)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_lineups_team ON lineups (team_id)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_lineups_hash ON lineups (team_id, lineup_hash)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_lineup_segments_game_team ON lineup_segments (game_id, team_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_lineup_segments_game_lineup ON lineup_segments (game_id, lineup_id)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_lineup_segments_lineup ON lineup_segments (lineup_id)")


def main() -> None:
    for database_path in DATABASE_PATHS:
        if not database_path.exists():
            continue

        with connect(database_path) as connection:
            migrate_events_table(connection)
            create_lineups_tables(connection)
            create_indexes(connection)
            connection.execute("PRAGMA foreign_keys = ON;")
            connection.commit()


if __name__ == "__main__":
    main()
