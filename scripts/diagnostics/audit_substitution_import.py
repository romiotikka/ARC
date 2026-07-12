from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lineup_segment_generator import build_player_lookup
from lineup_segment_generator import connect
from lineup_segment_generator import load_game_rows
from lineup_segment_generator import normalize_text
from lineup_segment_generator import resolve_player_id


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"
FETCH_TIMEOUT_SECONDS = 30


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def load_source_substitutions(connection, game_row, player_lookup: dict[str, str]) -> list[dict]:
    data = fetch_json(game_row["json_url"])
    team_ids_by_livestats_number = {
        "1": normalize_text(game_row["home_team_id"]),
        "2": normalize_text(game_row["away_team_id"]),
    }

    substitutions: list[dict] = []
    for index, event in enumerate(data.get("pbp", []), start=1):
        action_type = (normalize_text(event.get("actionType")) or "").lower()
        if action_type != "substitution":
            continue

        action_number = int(event.get("actionNumber") or index)
        period = int(event.get("period") or 0)
        clock = normalize_text(event.get("clock")) or ""
        livestats_team_number = normalize_text(event.get("tno")) or ""
        team_id = team_ids_by_livestats_number.get(livestats_team_number)

        player_name = normalize_text(event.get("player"))
        player_id = resolve_player_id(connection, player_lookup, player_name) if player_name else None

        substitutions.append(
            {
                "action_number": action_number,
                "period": period,
                "clock": clock,
                "team": team_id,
                "team_number": livestats_team_number,
                "player_name": player_name,
                "player_id": player_id,
                "source_index": index,
            }
        )

    substitutions.sort(key=lambda row: (row["action_number"], row["source_index"]))
    return substitutions


def load_event_substitutions(connection, game_id: str, player_name_by_id: dict[str, str]) -> list[dict]:
    substitutions: list[dict] = []
    rows = connection.execute(
        """
        SELECT event_id, action_number, period, clock, team_id, player_id
        FROM events
        WHERE game_id = ?
          AND event_type = 'substitution'
        ORDER BY action_number, event_id
        """,
        (game_id,),
    ).fetchall()

    for row in rows:
        player_id = normalize_text(row[5])
        substitutions.append(
            {
                "action_number": int(row[1] or 0),
                "period": int(row[2] or 0),
                "clock": normalize_text(row[3]) or "",
                "team": normalize_text(row[4]) or "",
                "player_name": player_name_by_id.get(player_id or "", ""),
                "player_id": player_id,
                "event_id": int(row[0]),
            }
        )

    return substitutions


def build_key(row: dict) -> tuple[int, int, str, str, str, str]:
    return (
        int(row["action_number"]),
        int(row["period"]),
        normalize_text(row["clock"]) or "",
        normalize_text(row["team"]) or "",
        normalize_text(row["player_name"]) or "",
        normalize_text(row["player_id"]) or "",
    )


def compare_game(source_rows: list[dict], event_rows: list[dict]) -> dict[str, object]:
    result = {
        "exact_match": False,
        "wrong_ordering": False,
        "missing_rows": False,
        "extra_rows": False,
        "wrong_player_mapping": False,
        "same_clock_group_mismatch": False,
        "same_clock_group_ordering": False,
        "name_only_mismatch": False,
        "source_count": 0,
        "event_count": 0,
    }

    source_primary = [
        (
            build_key(row)[0],
            build_key(row)[1],
            build_key(row)[2],
            build_key(row)[3],
            build_key(row)[5],
        )
        for row in source_rows
    ]
    event_primary = [
        (
            build_key(row)[0],
            build_key(row)[1],
            build_key(row)[2],
            build_key(row)[3],
            build_key(row)[5],
        )
        for row in event_rows
    ]
    result["source_count"] = len(source_primary)
    result["event_count"] = len(event_primary)
    source_counter = Counter(source_primary)
    event_counter = Counter(event_primary)

    if source_primary == event_primary:
        result["exact_match"] = True
    else:
        if source_counter == event_counter:
            result["wrong_ordering"] = True
        if source_counter - event_counter:
            result["missing_rows"] = True
        if event_counter - source_counter:
            result["extra_rows"] = True

    source_by_action = defaultdict(list)
    event_by_action = defaultdict(list)
    for row in source_rows:
        source_by_action[(row["action_number"], row["period"], row["clock"], row["team"])].append(row)
    for row in event_rows:
        event_by_action[(row["action_number"], row["period"], row["clock"], row["team"])].append(row)

    if any(key in event_by_action and Counter(item["player_id"] or "" for item in rows) != Counter(item["player_id"] or "" for item in event_by_action[key]) for key, rows in source_by_action.items()):
        result["wrong_player_mapping"] = True

    source_same_clock = defaultdict(list)
    event_same_clock = defaultdict(list)
    for row in source_rows:
        source_same_clock[(row["period"], row["clock"], row["team"])].append(row)
    for row in event_rows:
        event_same_clock[(row["period"], row["clock"], row["team"])].append(row)

    multi_keys = [key for key, rows in source_same_clock.items() if len(rows) > 1]
    if multi_keys:
        if any(len(source_same_clock[key]) != len(event_same_clock.get(key, [])) for key in multi_keys):
            result["same_clock_group_mismatch"] = True
        elif any(
            [normalize_text(row["player_id"]) or "" for row in source_same_clock[key]]
            != [normalize_text(row["player_id"]) or "" for row in event_same_clock.get(key, [])]
            for key in multi_keys
        ):
            result["same_clock_group_ordering"] = True

    if any(
        normalize_text(source_row["player_name"]) != normalize_text(event_row["player_name"])
        for source_row, event_row in zip(source_rows, event_rows)
        if (
            build_key(source_row)[0],
            build_key(source_row)[1],
            build_key(source_row)[2],
            build_key(source_row)[3],
            build_key(source_row)[5],
        )
        == (
            build_key(event_row)[0],
            build_key(event_row)[1],
            build_key(event_row)[2],
            build_key(event_row)[3],
            build_key(event_row)[5],
        )
    ):
        result["name_only_mismatch"] = True

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit substitution import against LiveStats pbp")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH), help="SQLite database path")
    args = parser.parse_args()

    connection = connect(Path(args.database))
    try:
        player_lookup = build_player_lookup(connection)
        player_name_by_id = {str(row[0]): str(row[1]) for row in connection.execute("SELECT player_id, canonical_name FROM players")}
        game_rows = load_game_rows(connection)

        skipped_fetch: list[tuple[str, int]] = []
        exact_match_count = 0
        category_counts = Counter()
        category_examples: dict[str, list[str]] = defaultdict(list)
        name_only_count = 0

        for game_row in game_rows:
            try:
                source_rows = load_source_substitutions(connection, game_row, player_lookup)
            except HTTPError as exc:
                skipped_fetch.append((game_row["game_id"], int(exc.code)))
                continue

            event_rows = load_event_substitutions(connection, game_row["game_id"], player_name_by_id)
            comparison = compare_game(source_rows, event_rows)

            if comparison["exact_match"]:
                exact_match_count += 1
                continue

            if comparison["name_only_mismatch"]:
                name_only_count += 1

            for category in (
                "missing_rows",
                "extra_rows",
                "wrong_player_mapping",
                "wrong_ordering",
                "same_clock_group_mismatch",
                "same_clock_group_ordering",
            ):
                if comparison[category]:
                    category_counts[category] += 1
                    category_examples[category].append(game_row["game_id"])

        print(f"games_total={len(game_rows)}")
        print(f"games_exact_match={exact_match_count}")
        print(f"games_skipped_fetch={len(skipped_fetch)}")
        if skipped_fetch:
            examples = ",".join(f"{game_id}:{code}" for game_id, code in skipped_fetch[:10])
            print(f"skipped_fetch_examples={examples}")

        for category in (
            "missing_rows",
            "extra_rows",
            "wrong_player_mapping",
            "wrong_ordering",
            "same_clock_group_mismatch",
            "same_clock_group_ordering",
        ):
            print(f"{category}={category_counts[category]}")
            if category_examples[category]:
                print(f"{category}_examples={','.join(category_examples[category][:10])}")

        print(f"name_only_mismatch={name_only_count}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()