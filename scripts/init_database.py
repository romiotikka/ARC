from __future__ import annotations

import csv
import re
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DATABASE_DIR = ROOT_DIR / "data"
DATABASE_PATH = DATABASE_DIR / "arc.db"
SCHEMA_PATH = ROOT_DIR / "database" / "schema.sql"
ESTLATBL_GAMES_CSV = ROOT_DIR / "estlatbl_2026_games.csv"

ESTLATBL_LEAGUE_ID = 1
ESTLATBL_SEASON_ID = 20252026
LIVE_STATS_URL = "https://www.estlatbl.com/et/tulemused/{}/live-stats"
GAME_DATE_PATTERN = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")


def parse_estlatbl_game_date(html: str) -> str | None:
    match = GAME_DATE_PATTERN.search(html)
    if not match:
        return None

    day, month, year = match.groups()
    year = int(year)
    if year < 100:
        year += 2000

    return f"{year:04d}-{int(month):02d}-{int(day):02d}"


def scrape_live_stats_game_date(provider_game_id: str) -> str | None:
    url = LIVE_STATS_URL.format(provider_game_id)
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"Warning: failed to fetch live-stats page for {provider_game_id}: {exc}")
        return None

    date = parse_estlatbl_game_date(html)
    if date is None:
        print(f"Warning: could not parse game_date from live-stats HTML for {provider_game_id}")
    return date



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
            game_id = str(provider_game_id)
            game_date = scrape_live_stats_game_date(provider_game_id)

            connection.execute(
                """
                INSERT INTO games (game_id, league_id, season_id, game_date, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (game_id) DO UPDATE SET
                    league_id = excluded.league_id,
                    season_id = excluded.season_id,
                    game_date = excluded.game_date,
                    status = excluded.status
                """,
                (
                    game_id,
                    ESTLATBL_LEAGUE_ID,
                    ESTLATBL_SEASON_ID,
                    game_date,
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

