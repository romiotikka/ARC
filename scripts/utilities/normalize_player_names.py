"""Review persisted LiveStats player identity decisions without changing them.

Canonical names and aliases are managed through the Phase 1 identity workflow.
This utility intentionally performs no provider requests and no database writes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "arc2.db"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--game-id", help="Limit the review report to one ARC game")
    args = parser.parse_args()
    database_path = args.database.resolve()
    if not database_path.is_file():
        raise SystemExit(f"Database does not exist: {database_path}")

    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        sql = """SELECT s.game_id, po.player_occurrence_id, po.original_name,
                        po.first_name, po.last_name, po.jersey_number,
                        po.provider_player_id, po.pno, d.status, d.evidence_json
                 FROM identity_decisions d
                 JOIN source_player_occurrences po ON po.player_occurrence_id=d.player_occurrence_id
                 JOIN source_snapshot_normalizations n ON n.normalization_id=po.normalization_id
                 JOIN source_game_snapshots s ON s.snapshot_id=n.snapshot_id
                 WHERE d.status <> 'resolved'"""
        parameters: tuple[object, ...] = ()
        if args.game_id:
            sql += " AND s.game_id = ?"
            parameters = (args.game_id,)
        sql += " ORDER BY s.game_id, po.source_ordinal"
        rows = connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()

    for row in rows:
        record = dict(row)
        if record["evidence_json"]:
            try:
                record["evidence_json"] = json.loads(record["evidence_json"])
            except json.JSONDecodeError:
                pass
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    print(f"reviewed={len(rows)}; no identity records were changed")


if __name__ == "__main__":
    main()
