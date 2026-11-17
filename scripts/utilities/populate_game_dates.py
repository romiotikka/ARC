"""Run the EstLatBL results collector against one explicitly selected database.

The old utility scraped the first date-looking string from a LiveStats page and
updated arc.db and arc2.db automatically. That path was ambiguous and unsafe.
The collector now parses completed rows from official season results pages and
validates each date before persistence.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


ROOT_DIR = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT_DIR / "scripts" / "import" / "collect_estlatbl_seasons.mjs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        required=True,
        type=Path,
        help="ARC database to update; choose a disposable database for validation",
    )
    parser.add_argument(
        "--recent-count",
        type=int,
        help="Collect only this many newest configured seasons",
    )
    args = parser.parse_args()
    database_path = args.database.expanduser().resolve()
    if not database_path.is_file():
        parser.error(f"database does not exist: {database_path}")
    if args.recent_count is not None and args.recent_count < 1:
        parser.error("--recent-count must be positive")

    environment = os.environ.copy()
    environment["ARC_DATABASE_PATH"] = str(database_path)
    command = ["node", str(COLLECTOR)]
    if args.recent_count is not None:
        command.extend(["--recent", "--recent-count", str(args.recent_count)])
    return subprocess.run(command, cwd=ROOT_DIR, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
