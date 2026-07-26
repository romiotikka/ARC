from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sqlite3
import unicodedata


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def rows_by_normalized_value(rows: list[sqlite3.Row], value_key: str, player_key: str) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        normalized = normalize_name(row[value_key])
        if normalized:
            grouped[normalized].add(str(row[player_key]))
    return grouped


def collect_alias_conflicts(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    players = connection.execute(
        "SELECT player_id, canonical_name FROM players ORDER BY player_id"
    ).fetchall()
    aliases = connection.execute(
        "SELECT alias_id, player_id, alias_name, source FROM player_aliases ORDER BY alias_id"
    ).fetchall()

    aliases_by_normalized = rows_by_normalized_value(aliases, "alias_name", "player_id")
    aliases_by_player_normalized: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for alias in aliases:
        normalized = normalize_name(alias["alias_name"])
        if normalized:
            aliases_by_player_normalized[(str(alias["player_id"]), normalized)].append(alias)

    canonical_by_normalized = rows_by_normalized_value(players, "canonical_name", "player_id")
    player_names = {str(row["player_id"]): row["canonical_name"] for row in players}

    aliases_mapping_to_multiple_players = []
    for normalized, player_ids in sorted(aliases_by_normalized.items()):
        if len(player_ids) > 1:
            matching_aliases = [
                {
                    "player_id": str(row["player_id"]),
                    "canonical_name": player_names.get(str(row["player_id"])),
                    "alias_name": row["alias_name"],
                    "source": row["source"],
                }
                for row in aliases
                if normalize_name(row["alias_name"]) == normalized
            ]
            aliases_mapping_to_multiple_players.append(
                {"normalized_alias": normalized, "aliases": matching_aliases}
            )

    duplicate_aliases_within_player = []
    for (player_id, normalized), matching_aliases in sorted(aliases_by_player_normalized.items()):
        distinct_aliases = sorted({str(row["alias_name"]) for row in matching_aliases})
        if len(distinct_aliases) > 1:
            duplicate_aliases_within_player.append(
                {
                    "player_id": player_id,
                    "canonical_name": player_names.get(player_id),
                    "normalized_alias": normalized,
                    "aliases": distinct_aliases,
                }
            )

    duplicate_canonical_names = []
    for normalized, player_ids in sorted(canonical_by_normalized.items()):
        if len(player_ids) > 1:
            duplicate_canonical_names.append(
                {
                    "normalized_canonical_name": normalized,
                    "players": [
                        {"player_id": player_id, "canonical_name": player_names[player_id]}
                        for player_id in sorted(player_ids)
                    ],
                }
            )

    canonical_names_used_as_other_player_aliases = []
    for canonical_normalized, canonical_player_ids in sorted(canonical_by_normalized.items()):
        alias_player_ids = aliases_by_normalized.get(canonical_normalized, set())
        conflicting_alias_player_ids = alias_player_ids - canonical_player_ids
        if conflicting_alias_player_ids:
            canonical_names_used_as_other_player_aliases.append(
                {
                    "normalized_name": canonical_normalized,
                    "canonical_players": [
                        {"player_id": player_id, "canonical_name": player_names[player_id]}
                        for player_id in sorted(canonical_player_ids)
                    ],
                    "alias_players": [
                        {"player_id": player_id, "canonical_name": player_names.get(player_id)}
                        for player_id in sorted(conflicting_alias_player_ids)
                    ],
                }
            )

    return {
        "aliases_mapping_to_multiple_players": aliases_mapping_to_multiple_players,
        "normalized_duplicate_aliases_within_player": duplicate_aliases_within_player,
        "duplicate_normalized_canonical_names": duplicate_canonical_names,
        "canonical_names_used_as_other_player_aliases": canonical_names_used_as_other_player_aliases,
    }


def collect_roster_conflicts(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    player_games_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(player_games)").fetchall()
    }
    provider_player_id_column = (
        "provider_player_id"
        if "provider_player_id" in player_games_columns
        else "NULL AS provider_player_id"
    )
    rows = connection.execute(
        f"""
        SELECT
            game_id,
            COALESCE(team_id, team_name) AS roster_team,
            team_name,
            player_id,
            player_name,
            shirt_number,
            {provider_player_id_column}
        FROM player_games
        ORDER BY game_id, roster_team, player_game_id
        """
    ).fetchall()

    same_name_on_roster: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    same_jersey_on_roster: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    provider_id_to_players: dict[str, set[str]] = defaultdict(set)
    game_player_to_teams: dict[tuple[str, str], set[str]] = defaultdict(set)
    game_name_to_players: dict[tuple[str, str], set[str]] = defaultdict(set)
    player_id_to_names: dict[str, set[str]] = defaultdict(set)
    unresolved_rows: list[dict] = []

    for row in rows:
        game_id = str(row["game_id"])
        roster_team = str(row["roster_team"] or "")
        player_id = str(row["player_id"]) if row["player_id"] is not None else None
        player_name = str(row["player_name"])
        normalized_name = normalize_name(player_name)
        shirt_number = str(row["shirt_number"] or "").strip()

        if player_id is None:
            unresolved_rows.append(
                {
                    "game_id": game_id,
                    "roster_team": roster_team,
                    "player_name": player_name,
                    "shirt_number": shirt_number or None,
                    "provider_player_id": row["provider_player_id"],
                }
            )
            continue

        same_name_on_roster[(game_id, roster_team, normalized_name)].append(row)
        if shirt_number:
            same_jersey_on_roster[(game_id, roster_team, shirt_number)].append(row)
        if row["provider_player_id"] is not None:
            provider_id_to_players[str(row["provider_player_id"])].add(player_id)
        game_player_to_teams[(game_id, player_id)].add(roster_team)
        game_name_to_players[(game_id, normalized_name)].add(player_id)
        player_id_to_names[player_id].add(normalized_name)

    same_name_players = []
    for (game_id, roster_team, normalized_name), matching_rows in sorted(same_name_on_roster.items()):
        player_ids = {str(row["player_id"]) for row in matching_rows}
        if len(player_ids) > 1:
            same_name_players.append(
                {
                    "game_id": game_id,
                    "roster_team": roster_team,
                    "normalized_name": normalized_name,
                    "rows": [
                        {
                            "player_id": str(row["player_id"]),
                            "player_name": row["player_name"],
                            "shirt_number": row["shirt_number"],
                            "provider_player_id": row["provider_player_id"],
                        }
                        for row in matching_rows
                    ],
                }
            )

    jersey_conflicts = []
    for (game_id, roster_team, shirt_number), matching_rows in sorted(same_jersey_on_roster.items()):
        player_ids = {str(row["player_id"]) for row in matching_rows}
        if len(player_ids) > 1:
            jersey_conflicts.append(
                {
                    "game_id": game_id,
                    "roster_team": roster_team,
                    "shirt_number": shirt_number,
                    "rows": [
                        {
                            "player_id": str(row["player_id"]),
                            "player_name": row["player_name"],
                            "provider_player_id": row["provider_player_id"],
                        }
                        for row in matching_rows
                    ],
                }
            )

    provider_id_conflicts = [
        {"provider_player_id": provider_id, "player_ids": sorted(player_ids)}
        for provider_id, player_ids in sorted(provider_id_to_players.items())
        if len(player_ids) > 1
    ]

    player_on_both_teams = [
        {"game_id": game_id, "player_id": player_id, "roster_teams": sorted(teams)}
        for (game_id, player_id), teams in sorted(game_player_to_teams.items())
        if len(teams) > 1
    ]

    same_game_name_multiple_players = [
        {"game_id": game_id, "normalized_name": name, "player_ids": sorted(player_ids)}
        for (game_id, name), player_ids in sorted(game_name_to_players.items())
        if len(player_ids) > 1
    ]

    player_ids_with_multiple_names = [
        {"player_id": player_id, "normalized_player_names": sorted(names)}
        for player_id, names in sorted(player_id_to_names.items())
        if len(names) > 1
    ]

    return {
        "same_name_players_on_same_roster": same_name_players,
        "jersey_conflicts_on_same_roster": jersey_conflicts,
        "provider_player_id_mapping_to_multiple_players": provider_id_conflicts,
        "same_player_id_on_both_teams_in_one_game": player_on_both_teams,
        "same_normalized_name_mapping_to_multiple_players_in_one_game": same_game_name_multiple_players,
        "player_ids_with_multiple_normalized_player_game_names": player_ids_with_multiple_names,
        "player_games_without_player_id": unresolved_rows,
    }


def collect_orphaned_aliases(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT a.alias_id, a.player_id, a.alias_name, a.source
        FROM player_aliases a
        LEFT JOIN players p ON p.player_id = a.player_id
        WHERE p.player_id IS NULL
        ORDER BY a.alias_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only audit of player and player_aliases identity conflicts"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH), help="SQLite database path")
    args = parser.parse_args()

    database_path = Path(args.database)
    if not database_path.is_file():
        parser.error(f"database not found: {database_path}")

    connection = connect_read_only(database_path)
    try:
        alias_conflicts = collect_alias_conflicts(connection)
        roster_conflicts = collect_roster_conflicts(connection)
        orphaned_aliases = collect_orphaned_aliases(connection)
    finally:
        connection.close()

    findings = {**alias_conflicts, **roster_conflicts, "orphaned_player_aliases": orphaned_aliases}
    summary = {category: len(items) for category, items in findings.items()}
    print(json.dumps({"database": str(database_path), "read_only": True, "summary": summary, "findings": findings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
