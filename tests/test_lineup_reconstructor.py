from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts" / "core"))

from lineup_reconstructor import get_or_create_lineup, lineup_hash_for_players


class LineupReconstructorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        schema = (ROOT_DIR / "database" / "schema.sql").read_text()
        self.db.executescript(schema)
        self.db.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'Test League')")
        self.db.execute("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (1, '2022-23', 2022, 2023)")
        self.db.execute("INSERT INTO teams (team_id, canonical_name) VALUES (2, 'Team 2')")
        for pid in range(1, 6):
            self.db.execute(
                "INSERT INTO players (player_id, canonical_name) VALUES (?, ?)",
                (pid, f"Player {pid}"),
            )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()

    def test_get_or_create_lineup_reuses_existing_db_lineup_when_cache_empty(self) -> None:
        players = ["1", "2", "3", "4", "5"]
        h = lineup_hash_for_players(players)
        self.db.execute(
            """
            INSERT INTO lineups (team_id, player_1, player_2, player_3, player_4, player_5, lineup_hash)
            VALUES (2, 1, 2, 3, 4, 5, ?)
            """,
            (h,),
        )
        self.db.commit()

        # Cache is empty (simulating cache miss or type mismatch)
        cache: dict[tuple[str, str], int] = {}
        lineup_id, created = get_or_create_lineup(self.db, cache, "2", players)

        self.assertFalse(created)
        self.assertEqual(lineup_id, 1)
        self.assertEqual(cache.get(("2", h)), 1)
        # Ensure no second row was inserted
        count = self.db.execute("SELECT COUNT(*) FROM lineups").fetchone()[0]
        self.assertEqual(count, 1)

    def test_get_or_create_lineup_string_and_int_team_id_compatibility(self) -> None:
        players = ["1", "2", "3", "4", "5"]
        cache: dict[tuple[str, str], int] = {}

        # First call with str team_id creates lineup
        lid1, created1 = get_or_create_lineup(self.db, cache, "2", players)
        self.assertTrue(created1)

        # Second call with int team_id reuses lineup
        lid2, created2 = get_or_create_lineup(self.db, cache, 2, players)
        self.assertFalse(created2)
        self.assertEqual(lid1, lid2)


if __name__ == "__main__":
    unittest.main()
