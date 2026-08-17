"""JSON-lines bridge from the LiveStats Node importer to IdentityResolver.

The importer owns source parsing; this program owns no matching policy.  It
only turns parser-supplied occurrence contexts into resolver calls and returns
the resulting ARC player IDs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.identity.exceptions import ManualReviewRequired
from scripts.identity.models import IdentityContext
from scripts.identity.resolver import IdentityResolver
from scripts.identity.utils import normalize_position


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.load(sys.stdin)
    occurrences = payload.get("occurrences")
    if not isinstance(occurrences, list):
        raise ValueError("Expected an object with an 'occurrences' list")

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    resolver = IdentityResolver(connection)
    resolved: dict[str, dict[str, object]] = {}

    try:
        for occurrence in occurrences:
            key = occurrence.get("key")
            raw_name = occurrence.get("raw_name")
            if not isinstance(key, str) or not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError("Each occurrence requires non-empty string key and raw_name")

            context = IdentityContext(
                raw_name=raw_name,
                team_id=str(occurrence["team_id"]),
                season_id=int(occurrence["season_id"]),
                league_id=int(occurrence["league_id"]),
                jersey_number=_optional_text(occurrence.get("jersey_number")),
                game_id=_optional_text(occurrence.get("game_id")),
                provider=_optional_text(occurrence.get("provider")) or "fiba_livestats",
                external_player_id=_optional_text(occurrence.get("external_player_id")),
                position=normalize_position(_optional_text(occurrence.get("position"))),
            )
            result = resolver.resolve(context)
            if result.player_id is None:
                raise ManualReviewRequired(f"No ARC player_id returned for {key!r}")
            resolved[key] = {
                "player_id": result.player_id,
                "status": result.status.value,
                "confidence": result.confidence,
            }
    finally:
        connection.close()

    json.dump({"resolved": resolved}, sys.stdout)
    return 0


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ManualReviewRequired, ValueError, sqlite3.Error) as exc:
        print(f"Identity resolution failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
