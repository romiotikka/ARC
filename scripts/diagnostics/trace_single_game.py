from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "scripts" / "core"))

from lineup_reconstructor import connect, run_trace_mode


DEFAULT_DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trace lineup reconstruction substitutions for one game"
    )
    parser.add_argument("--game-id", required=True, help="ARC game id to trace")
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE_PATH),
        help="SQLite database path",
    )
    args = parser.parse_args()

    connection = connect(Path(args.database))
    try:
        run_trace_mode(connection, args.game_id)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
