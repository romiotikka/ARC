from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATABASE_DIR = ROOT_DIR / "data"
DATABASE_PATH = DATABASE_DIR / "arc2.db"
SCHEMA_PATH = ROOT_DIR / "database" / "schema.sql"
ESTLATBL_GAMES_CSV = ROOT_DIR / "data" / "estlatbl" / "estlatbl_2026_games.csv"

ESTLATBL_LEAGUE_ID = 1
ESTLATBL_SEASON_ID = 20252026
ESTLATBL_LEAGUE_KEY = "estlatbl"

def connect() -> sqlite3.Connection:
    DATABASE_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def apply_schema(connection: sqlite3.Connection) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection.executescript(schema)


def seed_reference_data(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO leagues (league_id, name, country, region)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (league_id) DO UPDATE SET
            name = excluded.name,
            country = excluded.country,
            region = excluded.region
        """,
        (
            ESTLATBL_LEAGUE_ID,
            "Estonian-Latvian Basketball League",
            "Estonia/Latvia",
            "Northern Europe",
        ),
    )

    connection.execute(
        """
        INSERT INTO seasons (season_id, label, start_year, end_year)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (season_id) DO UPDATE SET
            label = excluded.label,
            start_year = excluded.start_year,
            end_year = excluded.end_year
        """,
        (
            ESTLATBL_SEASON_ID,
            "2025-26",
            2025,
            2026,
        ),
    )


def import_estlatbl_games(connection: sqlite3.Connection) -> int:
    imported = 0

    with ESTLATBL_GAMES_CSV.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            provider_game_id = row["game_id"]
            match_id = row["match_id"]
            json_url = row["json_url"]
            game_id = f"{ESTLATBL_LEAGUE_KEY}_{provider_game_id}"

            connection.execute(
                """
                INSERT INTO games (game_id, league_id, season_id, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (game_id) DO UPDATE SET
                    league_id = excluded.league_id,
                    season_id = excluded.season_id,
                    status = excluded.status
                """,
                (
                    game_id,
                    ESTLATBL_LEAGUE_ID,
                    ESTLATBL_SEASON_ID,
                    "scheduled_or_import_pending",
                ),
            )

            connection.execute(
                """
                INSERT INTO source_livestats_games (
                    game_id,
                    league_id,
                    season_id,
                    provider_game_id,
                    match_id,
                    json_url
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (game_id) DO UPDATE SET
                    league_id = excluded.league_id,
                    season_id = excluded.season_id,
                    provider_game_id = excluded.provider_game_id,
                    match_id = excluded.match_id,
                    json_url = excluded.json_url
                """,
                (
                    game_id,
                    ESTLATBL_LEAGUE_ID,
                    ESTLATBL_SEASON_ID,
                    provider_game_id,
                    match_id,
                    json_url,
                ),
            )

            imported += 1

    return imported


def table_counts(connection: sqlite3.Connection) -> list[tuple[str, int]]:
    tables = [
        "leagues",
        "seasons",
        "games",
        "source_livestats_games",
        "teams",
        "players",
        "team_games",
        "player_games",
        "events",
        "lineups",
        "lineup_segments",
    ]

    counts = []
    for table in tables:
        count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts.append((table, count))

    return counts


def main() -> None:
    with connect() as connection:
        apply_schema(connection)
        seed_reference_data(connection)
        imported_games = import_estlatbl_games(connection)
        connection.commit()

        print(f"Database: {DATABASE_PATH}")
        print(f"Imported EstLatBL LiveStats mappings: {imported_games}")
        print()
        print("Table counts:")
        for table, count in table_counts(connection):
            print(f"- {table}: {count}")


if __name__ == "__main__":
    main()
