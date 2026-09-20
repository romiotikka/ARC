"""One-time destructive reset of the existing ARC database contents.

This script preserves the SQLite database file and schema. It is intentionally
not imported or run automatically by any production command.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"

# Verified against data/arc2.db on 2026-09-10. Delete children before parents.
DATA_TABLES = (
    "events",
    "lineup_segments",
    "lineups",
    "player_games",
    "team_games",
    "source_livestats_games",
    "team_aliases",
    "player_aliases",
    "player_external_ids",
    "team_external_ids",
    "season_external_ids",
    "league_external_ids",
    "games",
    "players",
    "teams",
    "seasons",
    "leagues",
)
OPTIONAL_DATA_TABLES = ("failed_imports",)


def main() -> None:
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")

        existing_tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        tables_to_clear = tuple(
            table_name
            for table_name in DATA_TABLES + OPTIONAL_DATA_TABLES
            if table_name in existing_tables
        )

        for table_name in tables_to_clear:
            connection.execute(f'DELETE FROM "{table_name}"')

        # Reset AUTOINCREMENT counters without changing the sqlite_sequence table.
        connection.execute("DELETE FROM sqlite_sequence")
        connection.commit()

        expected_tables = set(DATA_TABLES)
        actual_tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        missing_tables = expected_tables - actual_tables
        if missing_tables:
            raise RuntimeError(f"Reset verification missing tables: {sorted(missing_tables)}")

        non_empty = {}
        for table_name in tables_to_clear:
            count = connection.execute(
                f'SELECT COUNT(*) FROM "{table_name}"'
            ).fetchone()[0]
            if count != 0:
                non_empty[table_name] = count
        if non_empty:
            raise RuntimeError(f"Reset verification found remaining rows: {non_empty}")

        print(f"Reset complete: {DATABASE_PATH}")
        print(f"Verified empty tables: {len(tables_to_clear)}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
