"""Add Phase 2 source snapshot and reconstruction provenance tables.

Requires an explicit database path. This migration is additive and does not
backfill or fetch historical provider data.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]

ADDITIONAL_COLUMNS = {
    "source_event_occurrences": {
        "primary_identity_status": "TEXT",
        "secondary_identity_status": "TEXT",
    },
    "team_games": {
        "source_team_occurrence_id": "INTEGER REFERENCES source_team_occurrences(team_occurrence_id)",
    },
    "player_games": {
        "source_player_occurrence_id": "INTEGER REFERENCES source_player_occurrences(player_occurrence_id)",
        "identity_decision_id": "INTEGER REFERENCES identity_decisions(identity_decision_id)",
    },
    "events": {
        "source_event_occurrence_id": "INTEGER REFERENCES source_event_occurrences(event_occurrence_id)",
        "primary_source_player_occurrence_id": "INTEGER REFERENCES source_player_occurrences(player_occurrence_id)",
        "secondary_source_player_occurrence_id": "INTEGER REFERENCES source_player_occurrences(player_occurrence_id)",
        "primary_identity_decision_id": "INTEGER REFERENCES identity_decisions(identity_decision_id)",
        "secondary_identity_decision_id": "INTEGER REFERENCES identity_decisions(identity_decision_id)",
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    database_path = args.database.resolve()
    if not database_path.is_file():
        raise SystemExit(f"Database does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        schema = (ROOT_DIR / "database" / "schema.sql").read_text(encoding="utf-8")
        # CREATE TABLE/INDEX IF NOT EXISTS installs new objects; existing tables
        # are left untouched until nullable provenance columns are added below.
        connection.executescript("BEGIN IMMEDIATE;\n" + schema)
        for table, columns in ADDITIONAL_COLUMNS.items():
            present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in present:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f"Phase 2 schema installed additively: {database_path}")
    print("Existing rows remain unlinked and are not marked as original source snapshots.")


if __name__ == "__main__":
    main()
