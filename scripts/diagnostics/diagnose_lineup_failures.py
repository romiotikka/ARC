from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "scripts" / "core"))

from lineup_reconstructor import (
    build_player_lookup,
    connect,
    load_game_rows,
    process_game,
)

DEFAULT_DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostics for lineup reconstruction failures"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH), help="SQLite database path")
    args = parser.parse_args()

    connection = connect(Path(args.database))
    try:
        failed_games = load_game_rows(connection)
        player_lookup = build_player_lookup(connection)

        lineup_cache: dict[tuple[str, str], int] = {}
        for row in connection.execute("SELECT lineup_id, team_id, lineup_hash FROM lineups"):
            lineup_cache[(row[1], row[2])] = int(row[0])

        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)

        for game_row in failed_games:
            lineup_segment_cache: dict[str, int] = {}

            connection.execute("BEGIN IMMEDIATE")
            try:
                process_game(
                    connection,
                    game_row,
                    player_lookup,
                    lineup_cache,
                    lineup_segment_cache,
                )
                connection.rollback()
            except Exception as exc:  # noqa: BLE001 - exact original exception is required for diagnostics
                connection.rollback()
                exception_type = type(exc).__name__
                exception_message = str(exc)
                game_id = game_row["game_id"]

                print(f"game_id={game_id}")
                print(f"exception_type={exception_type}")
                print(f"exception_message={exception_message}")

                grouped[(exception_type, exception_message)].append(game_id)

        type_counts: dict[str, int] = defaultdict(int)
        for (exception_type, _), game_ids in grouped.items():
            type_counts[exception_type] += len(game_ids)

        for exception_type in sorted(type_counts):
            print(f"exception_type={exception_type}")
            print(f"games_count={type_counts[exception_type]}")

        for exception_type, exception_message in sorted(grouped):
            game_ids = grouped[(exception_type, exception_message)]
            examples = game_ids[:10]
            print(f"exception_type={exception_type}")
            print(f"exception_message={exception_message}")
            print(f"games_count={len(game_ids)}")
            print(f"example_game_ids={','.join(examples)}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()