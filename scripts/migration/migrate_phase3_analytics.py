"""Install Phase 3 LiveStats shot, event-link, and box-score fields additively."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]

ADDITIONAL_COLUMNS = {
    "source_event_occurrences": {
        "previous_action_number": "TEXT",
        "provider_shot_previous_action": "TEXT",
        "provider_shot_result": "INTEGER",
        "provider_shot_x": "REAL",
        "provider_shot_y": "REAL",
        "provider_shot_player_ref": "TEXT",
    },
    "events": {"coordinate_system": "TEXT"},
    "player_games": {
        "points_fast_break": "INTEGER",
        "points_in_paint": "INTEGER",
        "points_second_chance": "INTEGER",
        "blocks_received": "INTEGER",
        "personal_fouls": "INTEGER",
        "fouls_drawn": "INTEGER",
        "captain": "INTEGER",
    },
    "team_games": {
        "period_1_points": "INTEGER", "period_2_points": "INTEGER", "period_3_points": "INTEGER",
        "period_4_points": "INTEGER", "overtime_points": "INTEGER",
        "field_goals_made": "INTEGER", "field_goals_attempted": "INTEGER",
        "three_pointers_made": "INTEGER", "three_pointers_attempted": "INTEGER",
        "free_throws_made": "INTEGER", "free_throws_attempted": "INTEGER",
        "off_reb": "INTEGER", "def_reb": "INTEGER", "tot_reb": "INTEGER",
        "team_rebounds": "INTEGER", "team_off_reb": "INTEGER", "team_def_reb": "INTEGER",
        "assists": "INTEGER", "steals": "INTEGER", "blocks": "INTEGER", "blocks_received": "INTEGER",
        "turnovers": "INTEGER", "team_turnovers": "INTEGER", "personal_fouls": "INTEGER",
        "fouls_drawn": "INTEGER", "team_fouls": "INTEGER", "total_fouls": "INTEGER",
        "points_fast_break": "INTEGER", "points_from_turnovers": "INTEGER", "points_in_paint": "INTEGER",
        "points_second_chance": "INTEGER", "bench_points": "INTEGER", "biggest_lead": "INTEGER",
        "biggest_scoring_run": "INTEGER", "lead_changes": "INTEGER", "times_scores_level": "INTEGER",
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    database_path = parser.parse_args().database.resolve()
    if not database_path.is_file():
        raise SystemExit(f"Database does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        schema = (ROOT_DIR / "database" / "schema.sql").read_text(encoding="utf-8")
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

    print(f"Phase 3 analytics schema installed additively: {database_path}")


if __name__ == "__main__":
    main()
