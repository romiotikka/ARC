from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import urllib.request
from collections import defaultdict
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"
FETCH_TIMEOUT_SECONDS = 30

FAILURE_INVALID_SIZE = "INVALID_SIZE"
FAILURE_DUPLICATE_PLAYER = "DUPLICATE_PLAYER"
FAILURE_OUT_PLAYER_NOT_PRESENT = "OUT_PLAYER_NOT_PRESENT"
FAILURE_IN_PLAYER_ALREADY_PRESENT = "IN_PLAYER_ALREADY_PRESENT"
FAILURE_WRONG_TEAM_PLAYER = "WRONG_TEAM_PLAYER"
FAILURE_MISSING_STARTER = "MISSING_STARTER"
FAILURE_PERIOD_TRANSITION_ERROR = "PERIOD_TRANSITION_ERROR"
FAILURE_SUBSTITUTION_BATCH_ERROR = "SUBSTITUTION_BATCH_ERROR"
FAILURE_UNKNOWN = "UNKNOWN"
DEADBALL_EVENT_TYPES = {"timeout", "foul", "foulon", "freethrow", "jumpball", "jump_ball"}


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def table_has_column(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def ensure_duration_seconds_column(connection: sqlite3.Connection) -> None:
    if table_has_column(connection, "lineup_segments", "duration_seconds"):
        return

    connection.execute("ALTER TABLE lineup_segments ADD COLUMN duration_seconds INTEGER")


def normalize_text(value: object) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text if text else None


def normalize_player_key(value: object) -> str:
    text = normalize_text(value) or ""
    ascii_text = text.lower().encode("ascii", "ignore").decode("ascii")
    return "".join(character for character in ascii_text if character.isalnum())


def natural_player_sort_key(player_id: str) -> tuple[int, int | str]:
    if player_id.isdigit():
        return (0, int(player_id))
    return (1, player_id)


def clock_to_seconds(clock: str | None) -> int:
    if not clock:
        return 0

    parts = clock.split(":")
    if len(parts) < 2:
        return 0

    minutes = int(parts[0])
    seconds = int(parts[1])
    return minutes * 60 + seconds


def clock_duration_seconds(start_clock: str, end_clock: str) -> int:
    return clock_to_seconds(start_clock) - clock_to_seconds(end_clock)


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def build_player_lookup(connection: sqlite3.Connection) -> dict[str, str]:
    lookup: dict[str, str] = {}

    for row in connection.execute("SELECT player_id, canonical_name FROM players"):
        key = normalize_player_key(row[1])
        if key:
            lookup[key] = row[0]

    for row in connection.execute("SELECT player_id, alias_name FROM player_aliases"):
        key = normalize_player_key(row[1])
        if key:
            lookup[key] = row[0]

    return lookup


def resolve_player_id(connection: sqlite3.Connection, player_lookup: dict[str, str], player_name: str) -> str:
    normalized_name = normalize_text(player_name)
    if not normalized_name:
        raise ValueError("Missing player name")

    row = connection.execute(
        """
        SELECT player_id
        FROM players
        WHERE canonical_name = ? COLLATE NOCASE
        UNION
        SELECT player_id
        FROM player_aliases
        WHERE alias_name = ? COLLATE NOCASE
        LIMIT 1
        """,
        (normalized_name, normalized_name),
    ).fetchone()

    if row:
        return row[0]

    key = normalize_player_key(normalized_name)
    if key and key in player_lookup:
        return player_lookup[key]

    raise ValueError(f"Could not resolve player id for {player_name!r}")


def load_game_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url
        FROM games g
        JOIN source_livestats_games s ON s.game_id = g.game_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM lineup_segments ls
            WHERE ls.game_id = g.game_id
        )
        ORDER BY g.game_id
        """
    ).fetchall()


def load_validation_game(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url
        FROM games g
        JOIN source_livestats_games s ON s.game_id = g.game_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM lineup_segments ls
            WHERE ls.game_id = g.game_id
        )
        ORDER BY g.game_id
        LIMIT 1
        """
    ).fetchone()


def load_game_row_by_id(connection: sqlite3.Connection, game_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url
        FROM games g
        JOIN source_livestats_games s ON s.game_id = g.game_id
        WHERE g.game_id = ?
        LIMIT 1
        """,
        (game_id,),
    ).fetchone()


def extract_sorted_events(data: dict) -> list[tuple[int, dict]]:
    events = list(enumerate(data.get("pbp", []), start=1))
    events.sort(key=lambda item: (int(item[1].get("actionNumber") or item[0]), item[0]))
    return events


def group_period_events(events: list[tuple[int, dict]]) -> dict[int, list[tuple[int, dict]]]:
    grouped: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    for index, event in events:
        period = int(event.get("period") or 0)
        grouped[period].append((index, event))
    return grouped


def event_context_payload(index: int, event: dict, connection: sqlite3.Connection, game_id: str, player_lookup: dict[str, str], team_ids: dict[str, str]) -> dict:
    action_number = int(event.get("actionNumber") or index)
    period = int(event.get("period") or 0)
    clock = normalize_text(event.get("clock")) or ""
    action_type = (normalize_text(event.get("actionType")) or "").lower()
    team_number = normalize_text(event.get("tno")) or ""
    team_id = team_ids.get(team_number)

    player_name = normalize_text(event.get("player"))
    player_id = None
    if player_name:
        try:
            player_id = resolve_player_id(connection, player_lookup, player_name)
        except Exception:
            player_id = None

    event_row = connection.execute(
        """
                SELECT event_id, event_type, team_id, player_id, event_secondary_player_id, metadata_json
        FROM events
        WHERE game_id = ?
          AND action_number = ?
        LIMIT 1
        """,
        (game_id, action_number),
    ).fetchone()

    if event_row:
        event_id = int(event_row[0])
        event_type = normalize_text(event_row[1]) or action_type
        event_team_id = normalize_text(event_row[2]) or team_id
        event_player_id = normalize_text(event_row[3]) or player_id
        event_secondary_player_id = normalize_text(event_row[4])
        metadata_json = normalize_text(event_row[5])
    else:
        event_id = None
        event_type = action_type
        event_team_id = team_id
        event_player_id = player_id
        event_secondary_player_id = None
        metadata_json = None

    return {
        "index": index,
        "event_id": event_id,
        "action_number": action_number,
        "period": period,
        "clock": clock,
        "event_type": event_type,
        "team_id": event_team_id,
        "team_number": team_number,
        "player_id": event_player_id,
        "secondary_player_id": event_secondary_player_id,
        "metadata_json": metadata_json,
        "sub_type": (normalize_text(event.get("subType")) or "").lower(),
        "success": int(event.get("success") or 0),
        "live_payload_json": json.dumps(event, ensure_ascii=True, sort_keys=True),
    }


def classify_debug_root_cause(failure: dict, prior_sub_count_for_team: int) -> str:
    root = failure.get("root_cause")
    reason = (failure.get("reason") or "").lower()
    event_type = failure.get("event_type")
    operation = failure.get("operation")
    lineup_after = failure.get("lineup_after") or []
    period = int(failure.get("period") or 0)
    previous_event = failure.get("previous_event") or ""

    if root == FAILURE_WRONG_TEAM_PLAYER:
        return "F"

    if root == FAILURE_MISSING_STARTER:
        return "D"

    if root == FAILURE_PERIOD_TRANSITION_ERROR:
        return "E"

    if root == FAILURE_SUBSTITUTION_BATCH_ERROR:
        return "G"

    if root == FAILURE_OUT_PLAYER_NOT_PRESENT:
        return "B"

    if root == FAILURE_IN_PLAYER_ALREADY_PRESENT:
        return "C"

    if root == FAILURE_INVALID_SIZE and event_type == "substitution":
        if operation and operation.startswith("REMOVE"):
            return "A"
        if operation and operation.startswith("ADD"):
            return "B"
        if len(lineup_after) < 5:
            return "A"
        if len(lineup_after) > 5:
            return "B"

    if period > 1 and "period=" in previous_event:
        previous_period_text = previous_event.split("period=")[1].split("|")[0]
        if previous_period_text and int(previous_period_text) != period:
            return "E"

    if prior_sub_count_for_team == 0 and event_type == "substitution":
        return "D"

    if root == FAILURE_UNKNOWN and "http error" in reason:
        return "I"

    if root == FAILURE_UNKNOWN:
        return "I"

    return "I"


def validate_lineup_state(
    lineup: list[str],
    team_id: str,
    allowed_players: set[str],
) -> tuple[bool, str, str]:
    if len(lineup) != 5:
        return False, FAILURE_INVALID_SIZE, f"lineup size is {len(lineup)}"

    if len(set(lineup)) != len(lineup):
        return False, FAILURE_DUPLICATE_PLAYER, "duplicate player found in lineup"

    wrong_team_players = [player_id for player_id in lineup if player_id not in allowed_players]
    if wrong_team_players:
        return (
            False,
            FAILURE_WRONG_TEAM_PLAYER,
            f"players not on team {team_id}: {','.join(sorted(set(wrong_team_players), key=natural_player_sort_key))}",
        )

    return True, "", ""


def event_summary(context: dict | None) -> str:
    if context is None:
        return "none"
    return (
        f"period={context['period']}"
        f"|clock={context['clock']}"
        f"|action_number={context['action_number']}"
        f"|event_id={context['event_id']}"
        f"|event_type={context['event_type']}"
        f"|team_id={context['team_id']}"
        f"|player_id={context['player_id']}"
        f"|secondary_player_id={context['secondary_player_id']}"
    )


def format_lineup(lineup: list[str] | set[str]) -> str:
    players = sorted((normalize_text(player_id) or "" for player_id in lineup if normalize_text(player_id)), key=natural_player_sort_key)
    return ",".join(players)


def load_substitution_rows_for_action(connection: sqlite3.Connection, game_id: str, action_number: int) -> list[dict]:
    rows = connection.execute(
        """
        SELECT event_id, event_type, team_id, player_id, event_secondary_player_id, action_number, metadata_json
        FROM events
        WHERE game_id = ?
          AND action_number = ?
          AND event_type = 'substitution'
        ORDER BY event_id
        """,
        (game_id, action_number),
    ).fetchall()
    return [
        {
            "event_id": int(row["event_id"]),
            "event_type": normalize_text(row["event_type"]),
            "team_id": normalize_text(row["team_id"]),
            "player_id": normalize_text(row["player_id"]),
            "event_secondary_player_id": normalize_text(row["event_secondary_player_id"]),
            "action_number": int(row["action_number"]),
            "metadata_json": normalize_text(row["metadata_json"]),
        }
        for row in rows
    ]


def classify_trace_invalid_state(
    lineup_before: list[str],
    event: dict,
    team_id: str,
    resolved_player_id: str | None,
    operation_performed: str,
    allowed_players: dict[str, set[str]],
    duplicate_processing: bool,
    same_clock_events_for_team: list[dict],
) -> tuple[str, str]:
    if len(lineup_before) != 5:
        return "A", "lineup already had invalid size before this substitution"

    sub_type = (normalize_text(event.get("subType")) or "").lower()

    if sub_type == "out" and resolved_player_id and resolved_player_id not in set(lineup_before):
        return "B", f"outgoing player {resolved_player_id} was not on court"

    if sub_type == "in" and resolved_player_id is None:
        return "C", "incoming player could not be resolved (NULL)"

    if sub_type == "in" and operation_performed.startswith("SKIP_IN"):
        return "D", "incoming substitution was skipped"

    if resolved_player_id and resolved_player_id not in allowed_players.get(team_id, set()):
        return "G", f"resolved player {resolved_player_id} does not belong to team {team_id}"

    if duplicate_processing:
        return "H", "same substitution event appears to be processed more than once"

    if len(same_clock_events_for_team) > 1:
        has_out = any((normalize_text(item.get("subType")) or "").lower() == "out" for item in same_clock_events_for_team)
        has_in = any((normalize_text(item.get("subType")) or "").lower() == "in" for item in same_clock_events_for_team)
        if has_out and has_in:
            return "E", "same-clock substitution batch likely split/ordered incorrectly"

    return "I", "invalid lineup produced by substitution path not matched by A-H"


def run_trace_mode(connection: sqlite3.Connection, trace_game_id: str) -> None:
    game_row = load_game_row_by_id(connection, trace_game_id)
    if game_row is None:
        print("TRACE_ERROR")
        print(f"message=game not found: {trace_game_id}")
        return

    player_lookup = build_player_lookup(connection)
    game_data = fetch_json(game_row["json_url"])
    sorted_events = extract_sorted_events(game_data)

    home_team_id = normalize_text(game_row["home_team_id"]) or ""
    away_team_id = normalize_text(game_row["away_team_id"]) or ""
    team_ids = {"1": home_team_id, "2": away_team_id}

    allowed_players: dict[str, set[str]] = {home_team_id: set(), away_team_id: set()}
    for team_number in ("1", "2"):
        team_id = team_ids[team_number]
        team_payload = game_data.get("tm", {}).get(team_number) or {}
        for player in (team_payload.get("pl") or {}).values():
            player_name = normalize_text(player.get("name") or player.get("scoreboardName"))
            if not player_name:
                continue
            try:
                allowed_players[team_id].add(resolve_player_id(connection, player_lookup, player_name))
            except Exception:
                continue

    current_lineups: dict[str, list[str]] = {
        home_team_id: list(build_team_roster(game_data, "1", connection, player_lookup)),
        away_team_id: list(build_team_roster(game_data, "2", connection, player_lookup)),
    }

    for team_id in (home_team_id, away_team_id):
        if len(current_lineups[team_id]) != 5:
            print("FIRST_INVALID_STATE")
            print(f"game_id={trace_game_id}")
            print("period=0")
            print("clock=")
            print("action_number=0")
            print(f"lineup_before={format_lineup(current_lineups[team_id])}")
            print("operation_performed=starter_reconstruction")
            print(f"lineup_after={format_lineup(current_lineups[team_id])}")
            print("exact_explanation=starter reconstruction was already wrong")
            print("determination=F")
            return

    seen_substitutions: set[tuple[int, int, str, str, str]] = set()

    for index, event in sorted_events:
        action_type = (normalize_text(event.get("actionType")) or "").lower()
        if action_type != "substitution":
            continue

        team_number = normalize_text(event.get("tno")) or ""
        team_id = team_ids.get(team_number)
        if not team_id:
            continue

        period = int(event.get("period") or 0)
        clock = normalize_text(event.get("clock")) or ""
        action_number = int(event.get("actionNumber") or index)
        sub_type = (normalize_text(event.get("subType")) or "").lower()
        player_name = normalize_text(event.get("player"))
        secondary_player_id = normalize_text(event.get("secondaryPlayerId"))

        resolved_player_id = None
        resolve_error = None
        if player_name:
            try:
                resolved_player_id = resolve_player_id(connection, player_lookup, player_name)
            except Exception as exc:
                resolve_error = str(exc)

        lineup_before = list(current_lineups[team_id])
        lineup_after = list(lineup_before)
        lineup_after_set = set(lineup_after)

        duplicate_key = (index, action_number, team_id, sub_type, normalize_text(player_name) or "")
        duplicate_processing = duplicate_key in seen_substitutions
        seen_substitutions.add(duplicate_key)

        operation_performed = "NO_OP"
        resolved_out_player = resolved_player_id if sub_type == "out" else None
        resolved_in_player = resolved_player_id if sub_type == "in" else None

        if sub_type == "out":
            if not resolved_player_id:
                operation_performed = f"SKIP_OUT_UNRESOLVED({normalize_text(player_name) or ''})"
            elif resolved_player_id not in lineup_after_set:
                operation_performed = f"SKIP_OUT_NOT_ON_COURT({resolved_player_id})"
            else:
                lineup_after_set.remove(resolved_player_id)
                operation_performed = f"REMOVE({resolved_player_id})"
        elif sub_type == "in":
            if not resolved_player_id:
                operation_performed = f"SKIP_IN_RESOLVE_NULL({normalize_text(player_name) or ''})"
            elif resolved_player_id in lineup_after_set:
                operation_performed = f"SKIP_IN_ALREADY_PRESENT({resolved_player_id})"
            else:
                lineup_after_set.add(resolved_player_id)
                operation_performed = f"ADD({resolved_player_id})"
        else:
            operation_performed = f"SKIP_UNKNOWN_SUBTYPE({sub_type})"

        lineup_after = sorted(lineup_after_set, key=natural_player_sort_key)
        current_lineups[team_id] = list(lineup_after)
        lineup_size = len(lineup_after)

        same_clock_events_for_team = [
            probe_event
            for _, probe_event in sorted_events
            if (normalize_text(probe_event.get("actionType")) or "").lower() == "substitution"
            and int(probe_event.get("period") or 0) == period
            and (normalize_text(probe_event.get("clock")) or "") == clock
            and team_ids.get(normalize_text(probe_event.get("tno")) or "") == team_id
        ]

        print("TRACE_EVENT")
        print(f"period={period}")
        print(f"clock={clock}")
        print(f"action_number={action_number}")
        print("event_type=substitution")
        print(f"team_id={team_id}")
        print(f"player_id={normalize_text(resolved_player_id) or ''}")
        print(f"secondary_player_id={secondary_player_id or ''}")
        print(f"lineup_before={format_lineup(lineup_before)}")
        print(f"operation_performed={operation_performed}")
        print(f"lineup_after={format_lineup(lineup_after)}")
        print(f"lineup_size={lineup_size}")
        print(f"raw_livestats_payload={json.dumps(event, ensure_ascii=True, sort_keys=True)}")
        print(
            "raw_events_table_rows="
            + json.dumps(load_substitution_rows_for_action(connection, trace_game_id, action_number), ensure_ascii=True, sort_keys=True)
        )
        print(f"resolved_out_player={normalize_text(resolved_out_player) or ''}")
        print(f"resolved_in_player={normalize_text(resolved_in_player) or ''}")
        if resolve_error:
            print(f"resolve_error={resolve_error}")

        if lineup_size != 5:
            determination, explanation = classify_trace_invalid_state(
                lineup_before,
                event,
                team_id,
                resolved_player_id,
                operation_performed,
                allowed_players,
                duplicate_processing,
                same_clock_events_for_team,
            )
            print("FIRST_INVALID_STATE")
            print(f"game_id={trace_game_id}")
            print(f"period={period}")
            print(f"clock={clock}")
            print(f"action_number={action_number}")
            print(f"lineup_before={format_lineup(lineup_before)}")
            print(f"operation_performed={operation_performed}")
            print(f"lineup_after={format_lineup(lineup_after)}")
            print(f"exact_explanation={explanation}")
            print(f"determination={determination}")
            return

    print("FIRST_INVALID_STATE")
    print(f"game_id={trace_game_id}")
    print("period=")
    print("clock=")
    print("action_number=")
    print("lineup_before=")
    print("operation_performed=")
    print("lineup_after=")
    print("exact_explanation=no invalid lineup state encountered")
    print("determination=I")


def debug_first_invalid_event(
    connection: sqlite3.Connection,
    game_row: sqlite3.Row,
    player_lookup: dict[str, str],
) -> dict | None:
    game_id = game_row["game_id"]
    game_data = fetch_json(game_row["json_url"])
    sorted_events = extract_sorted_events(game_data)

    team_ids = {
        "1": normalize_text(game_row["home_team_id"]) or "",
        "2": normalize_text(game_row["away_team_id"]) or "",
    }

    allowed_players: dict[str, set[str]] = {team_ids["1"]: set(), team_ids["2"]: set()}
    for team_number in ("1", "2"):
        team_payload = game_data.get("tm", {}).get(team_number) or {}
        for player in (team_payload.get("pl") or {}).values():
            player_name = player.get("name") or player.get("scoreboardName")
            normalized_name = normalize_text(player_name)
            if not normalized_name:
                continue
            try:
                resolved = resolve_player_id(connection, player_lookup, normalized_name)
                allowed_players[team_ids[team_number]].add(resolved)
            except Exception:
                continue

    current_lineups = {
        team_ids["1"]: list(build_team_roster(game_data, "1", connection, player_lookup)),
        team_ids["2"]: list(build_team_roster(game_data, "2", connection, player_lookup)),
    }

    for team_id in (team_ids["1"], team_ids["2"]):
        valid, code, reason = validate_lineup_state(current_lineups[team_id], team_id, allowed_players[team_id])
        if not valid:
            return {
                "game_id": game_id,
                "root_cause": FAILURE_MISSING_STARTER if code == FAILURE_WRONG_TEAM_PLAYER else code,
                "reason": reason,
                "period": 0,
                "clock": "",
                "action_number": 0,
                "event_id": None,
                "event_type": "starter_validation",
                "team_id": team_id,
                "player_id": None,
                "secondary_player_id": None,
                "lineup_before": [],
                "lineup_after": list(current_lineups[team_id]),
                "previous_event": None,
                "next_event": event_summary(None if not sorted_events else event_context_payload(sorted_events[0][0], sorted_events[0][1], connection, game_id, player_lookup, team_ids)),
            }

    for position, (index, event) in enumerate(sorted_events):
        context = event_context_payload(index, event, connection, game_id, player_lookup, team_ids)
        before_lineups = {
            team_ids["1"]: list(current_lineups[team_ids["1"]]),
            team_ids["2"]: list(current_lineups[team_ids["2"]]),
        }

        reason_code = None
        reason_message = None
        event_team_id = context["team_id"]
        operation = None
        resolved_out_player = None
        resolved_in_player = None
        prior_sub_count_for_team = 0

        if event_team_id in current_lineups:
            for earlier_index, earlier_event in sorted_events[:position]:
                if (
                    (normalize_text(earlier_event.get("actionType")) or "").lower() == "substitution"
                    and team_ids.get(normalize_text(earlier_event.get("tno")) or "") == event_team_id
                ):
                    prior_sub_count_for_team += 1

        if context["event_type"] == "substitution" and event_team_id in current_lineups:
            lineup = current_lineups[event_team_id]
            player_id = normalize_text(context["player_id"]) or ""

            same_clock_count = 0
            for _, probe_event in sorted_events:
                if (
                    int(probe_event.get("period") or 0) == context["period"]
                    and (normalize_text(probe_event.get("clock")) or "") == context["clock"]
                    and team_ids.get(normalize_text(probe_event.get("tno")) or "") == event_team_id
                    and (normalize_text(probe_event.get("actionType")) or "").lower() == "substitution"
                ):
                    same_clock_count += 1

            if context["sub_type"] == "out":
                resolved_out_player = player_id
                operation = f"REMOVE({player_id})"
                if player_id not in lineup:
                    reason_code = FAILURE_OUT_PLAYER_NOT_PRESENT
                    reason_message = f"out player {player_id} not present"
                    if same_clock_count > 1:
                        reason_code = FAILURE_SUBSTITUTION_BATCH_ERROR
                        reason_message = f"batch out player {player_id} not present"
                else:
                    lineup.remove(player_id)
            elif context["sub_type"] == "in":
                resolved_in_player = player_id
                operation = f"ADD({player_id})"
                if player_id in lineup:
                    reason_code = FAILURE_IN_PLAYER_ALREADY_PRESENT
                    reason_message = f"in player {player_id} already present"
                    if same_clock_count > 1:
                        reason_code = FAILURE_SUBSTITUTION_BATCH_ERROR
                        reason_message = f"batch in player {player_id} already present"
                else:
                    lineup.append(player_id)
            else:
                reason_code = FAILURE_UNKNOWN
                reason_message = "missing substitution direction"

        after_lineups = {
            team_ids["1"]: list(current_lineups[team_ids["1"]]),
            team_ids["2"]: list(current_lineups[team_ids["2"]]),
        }

        if reason_code is None:
            for team_id in (team_ids["1"], team_ids["2"]):
                valid, code, reason = validate_lineup_state(after_lineups[team_id], team_id, allowed_players[team_id])
                if not valid:
                    reason_code = code
                    reason_message = reason
                    if (
                        position > 0
                        and int(sorted_events[position - 1][1].get("period") or 0) != context["period"]
                        and code in {FAILURE_INVALID_SIZE, FAILURE_DUPLICATE_PLAYER}
                    ):
                        reason_code = FAILURE_PERIOD_TRANSITION_ERROR
                        reason_message = f"period transition invalid state: {reason}"
                    event_team_id = team_id
                    break

        if reason_code is not None:
            previous_context = None
            if position > 0:
                previous_context = event_context_payload(
                    sorted_events[position - 1][0],
                    sorted_events[position - 1][1],
                    connection,
                    game_id,
                    player_lookup,
                    team_ids,
                )

            next_context = None
            if position + 1 < len(sorted_events):
                next_context = event_context_payload(
                    sorted_events[position + 1][0],
                    sorted_events[position + 1][1],
                    connection,
                    game_id,
                    player_lookup,
                    team_ids,
                )

            return {
                "game_id": game_id,
                "root_cause": reason_code,
                "reason": reason_message or "",
                "period": context["period"],
                "clock": context["clock"],
                "action_number": context["action_number"],
                "event_id": context["event_id"],
                "event_type": context["event_type"],
                "team_id": context["team_id"],
                "player_id": context["player_id"],
                "secondary_player_id": context["secondary_player_id"],
                "lineup_before": before_lineups.get(event_team_id, []),
                "lineup_after": after_lineups.get(event_team_id, []),
                "previous_event": event_summary(previous_context),
                "next_event": event_summary(next_context),
                "metadata_json": context["metadata_json"],
                "live_payload_json": context["live_payload_json"],
                "resolved_out_player": resolved_out_player,
                "resolved_in_player": resolved_in_player,
                "operation": operation,
                "classification": classify_debug_root_cause(
                    {
                        "root_cause": reason_code,
                        "reason": reason_message,
                        "event_type": context["event_type"],
                        "operation": operation,
                        "lineup_after": after_lineups.get(event_team_id, []),
                        "period": context["period"],
                        "previous_event": event_summary(previous_context),
                    },
                    prior_sub_count_for_team,
                ),
            }

    return None


def run_debug_mode(connection: sqlite3.Connection) -> None:
    player_lookup = build_player_lookup(connection)
    pending_games = load_game_rows(connection)

    games_processed = 0
    games_fixed = 0
    games_still_failing = 0
    failures: list[dict] = []

    for game_row in pending_games:
        games_processed += 1
        try:
            failure = debug_first_invalid_event(connection, game_row, player_lookup)
        except Exception as exc:
            failure = {
                "game_id": game_row["game_id"],
                "root_cause": FAILURE_UNKNOWN,
                "reason": str(exc),
                "period": 0,
                "clock": "",
                "action_number": 0,
                "event_id": None,
                "event_type": "debug_exception",
                "team_id": None,
                "player_id": None,
                "secondary_player_id": None,
                "lineup_before": [],
                "lineup_after": [],
                "previous_event": "none",
                "next_event": "none",
                "metadata_json": None,
                "live_payload_json": None,
                "resolved_out_player": None,
                "resolved_in_player": None,
                "operation": None,
                "classification": "I",
            }

        if failure is None:
            games_fixed += 1
            continue

        games_still_failing += 1
        failures.append(failure)

        print(f"game_id={failure['game_id']}")
        print(f"period={failure['period']}")
        print(f"clock={failure['clock']}")
        print(f"action_number={failure['action_number']}")
        print(f"event_id={failure['event_id']}")
        print(f"event_type={failure['event_type']}")
        print(f"team_id={failure['team_id']}")
        print(f"player_id={failure['player_id']}")
        print(f"secondary_player_id={failure['secondary_player_id']}")
        print(f"current_lineup_before_event={','.join(failure['lineup_before'])}")
        print(f"current_lineup_after_event={','.join(failure['lineup_after'])}")
        print(f"previous_event={failure['previous_event']}")
        print(f"next_event={failure['next_event']}")
        print(f"raw_metadata_json={failure['metadata_json']}")
        if failure["event_type"] == "substitution":
            print(f"livestats_substitution_payload={failure['live_payload_json']}")
            print(f"resolved_OUT_player={failure['resolved_out_player']}")
            print(f"resolved_IN_player={failure['resolved_in_player']}")
            print(f"reconstruction_operation_performed={failure['operation']}")
        print(f"reason_for_failure={failure['root_cause']}|{failure['reason']}")
        print(f"classified_root_cause={failure['classification']}")

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for failure in failures:
        grouped[(failure["classification"], failure["reason"])].append(failure)

    print(f"games_processed={games_processed}")
    print(f"games_fixed={games_fixed}")
    print(f"games_still_failing={games_still_failing}")

    for (root_cause, reason), group in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0][0], item[0][1])):
        example_game_ids = ",".join(item["game_id"] for item in group[:10])
        first = group[0]
        print(f"root_cause={root_cause}")
        print(f"count={len(group)}")
        print(f"example_game_ids={example_game_ids}")
        print(
            "first_invalid_event="
            f"game_id:{first['game_id']}|period:{first['period']}|clock:{first['clock']}|"
            f"action_number:{first['action_number']}|event_id:{first['event_id']}|event_type:{first['event_type']}|"
            f"team_id:{first['team_id']}|player_id:{first['player_id']}|secondary_player_id:{first['secondary_player_id']}|"
            f"reason:{first['reason']}"
        )


def build_team_roster(game_data: dict, team_number: str, connection: sqlite3.Connection, player_lookup: dict[str, str]) -> list[str]:
    team = game_data.get("tm", {}).get(team_number)
    if not team:
        raise ValueError(f"Missing team data for team number {team_number}")

    starters: list[str] = []
    for player in team.get("pl", {}).values():
        if int(player.get("starter") or 0) != 1:
            continue

        player_name = player.get("name") or player.get("scoreboardName")
        player_id = resolve_player_id(connection, player_lookup, player_name)
        starters.append(player_id)

    starters = sorted(set(starters), key=natural_player_sort_key)
    if len(starters) != 5:
        raise ValueError(f"Expected 5 starters for team {team_number}, found {len(starters)}")

    return starters


def lineup_hash_for_players(player_ids: list[str]) -> str:
    raw_value = "|".join(sorted(player_ids, key=natural_player_sort_key))
    return hashlib.sha1(raw_value.encode("utf-8")).hexdigest()


def get_or_create_lineup(
    connection: sqlite3.Connection,
    lineup_cache: dict[tuple[str, str], int],
    team_id: str,
    lineup_players: list[str],
) -> tuple[int, bool]:
    normalized_players = sorted(lineup_players, key=natural_player_sort_key)
    lineup_hash = lineup_hash_for_players(normalized_players)
    cache_key = (team_id, lineup_hash)

    if cache_key in lineup_cache:
        return lineup_cache[cache_key], False

    cursor = connection.execute(
        """
        INSERT INTO lineups (
            team_id,
            player_1,
            player_2,
            player_3,
            player_4,
            player_5,
            lineup_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (team_id, *normalized_players, lineup_hash),
    )
    lineup_id = int(cursor.lastrowid)
    lineup_cache[cache_key] = lineup_id
    return lineup_id, True


def apply_substitution_batch(
    current_lineup: set[str],
    substitutions: list[dict],
    team_label: str,
    period: int,
    clock: str,
    validate_size: bool = True,
) -> set[str]:
    ordered_substitutions = sorted(
        substitutions,
        key=lambda substitution: (int(substitution["actionNumber"]), int(substitution["index"])),
    )

    batch_out_players = [
        normalize_text(event.get("player_id"))
        for event in ordered_substitutions
        if (normalize_text(event.get("subType")) or "").lower() == "out"
    ]
    batch_in_players = [
        normalize_text(event.get("player_id"))
        for event in ordered_substitutions
        if (normalize_text(event.get("subType")) or "").lower() == "in"
    ]

    if (
        len(batch_out_players) == 1
        and len(batch_in_players) == 1
        and batch_out_players[0]
        and batch_in_players[0]
        and batch_out_players[0] in current_lineup
        and batch_in_players[0] in current_lineup
    ):
        print(
            f"Warning: treating ambiguous 1-out/1-in batch as no-op "
            f"for {team_label} period {period} clock {clock} "
            f"(out={batch_out_players[0]}, in={batch_in_players[0]})"
        )
        return current_lineup

    if (
        len(batch_out_players) == 1
        and len(batch_in_players) == 1
        and batch_out_players[0]
        and batch_in_players[0]
        and batch_out_players[0] not in current_lineup
        and batch_in_players[0] not in current_lineup
    ):
        print(
            f"Warning: treating ambiguous 1-out/1-in absent-player batch as no-op "
            f"for {team_label} period {period} clock {clock} "
            f"(out={batch_out_players[0]}, in={batch_in_players[0]})"
        )
        return current_lineup

    for event in ordered_substitutions:
        sub_type = normalize_text(event.get("subType"))
        player_id = normalize_text(event.get("player_id"))
        if not player_id:
            raise ValueError(f"Missing substitution player id for {team_label} period {period} clock {clock}")

        if sub_type == "out":
            if player_id not in current_lineup:
                print(
                    f"Warning: ignoring OUT for player {player_id} not on floor "
                    f"for {team_label} period {period} clock {clock}"
                )
                continue
            current_lineup.remove(player_id)
            continue

        if sub_type == "in":
            if player_id in current_lineup:
                print(
                    f"Warning: ignoring duplicate IN for player {player_id} already on floor "
                    f"for {team_label} period {period} clock {clock}"
                )
                continue
            current_lineup.add(player_id)
            continue

        raise ValueError(f"Missing substitution direction for {team_label} period {period} clock {clock}")

    if validate_size and len(current_lineup) != 5:
        raise ValueError(f"Invalid lineup size {len(current_lineup)} for {team_label} period {period} clock {clock}")

    return current_lineup


def find_next_incoming_before_resume(
    events_in_period: list[tuple[int, dict]],
    team_number: str,
    after_action_number: int,
) -> int | None:
    for index, event in sorted(events_in_period, key=lambda item: (int(item[1].get("actionNumber") or item[0]), item[0])):
        action_number = int(event.get("actionNumber") or index)
        if action_number <= after_action_number:
            continue

        action_type = (normalize_text(event.get("actionType")) or "").lower()
        if action_type == "substitution":
            event_team_number = normalize_text(event.get("tno")) or ""
            sub_type = (normalize_text(event.get("subType")) or "").lower()
            if event_team_number == team_number and sub_type == "in":
                return action_number
            continue

        if action_type in DEADBALL_EVENT_TYPES:
            continue

        return None

    return None


def prepare_team_substitutions(
    period_events: list[tuple[int, dict]],
    team_number: str,
    connection: sqlite3.Connection,
    player_lookup: dict[str, str],
) -> list[dict]:
    substitutions: list[dict] = []

    for index, event in period_events:
        action_type = normalize_text(event.get("actionType"))
        event_is_substitution = (action_type or "").lower() == "substitution"
        if not event_is_substitution:
            continue

        current_clock = normalize_text(event.get("clock")) or "00:00:00"

        if normalize_text(event.get("tno")) != team_number:
            continue

        sub_type = normalize_text(event.get("subType"))
        if sub_type not in {"in", "out"}:
            raise ValueError(f"Missing substitution direction for action {event.get('actionNumber')}")

        player_name = normalize_text(event.get("player"))
        if not player_name:
            raise ValueError(f"Missing substitution player name for action {event.get('actionNumber')}")

        substitutions.append(
            {
                "index": index,
                "actionNumber": int(event.get("actionNumber") or index),
                "clock": current_clock,
                "period": int(event.get("period") or 0),
                "subType": sub_type,
                "player_id": resolve_player_id(connection, player_lookup, player_name),
            }
        )

    return substitutions


def group_substitutions_by_batch(substitutions: list[dict]) -> list[list[dict]]:
    if not substitutions:
        return []

    grouped_by_key: dict[tuple[int, str], list[dict]] = {}
    key_order: list[tuple[int, str]] = []

    for substitution in substitutions:
        substitution_key = (substitution["period"], substitution["clock"])
        if substitution_key not in grouped_by_key:
            grouped_by_key[substitution_key] = []
            key_order.append(substitution_key)
        grouped_by_key[substitution_key].append(substitution)

    return [grouped_by_key[key] for key in key_order]


def insert_segment(
    connection: sqlite3.Connection,
    lineup_segment_cache: dict[str, int],
    lineup_id: int,
    game_id: str,
    team_id: str,
    period: int,
    start_clock: str,
    end_clock: str,
    start_action_number: int | None,
    end_action_number: int | None,
) -> tuple[int, bool]:
    cache_key = f"{game_id}|{team_id}|{period}|{start_clock}|{end_clock}|{lineup_id}"
    if cache_key in lineup_segment_cache:
        return lineup_segment_cache[cache_key], False

    duration_seconds = clock_duration_seconds(start_clock, end_clock)
    columns = [
        "game_id",
        "lineup_id",
        "team_id",
        "period",
        "start_action_number",
        "end_action_number",
        "start_clock",
        "end_clock",
    ]
    values = [
        game_id,
        lineup_id,
        team_id,
        period,
        start_action_number,
        end_action_number,
        start_clock,
        end_clock,
    ]

    if table_has_column(connection, "lineup_segments", "duration_seconds"):
        columns.append("duration_seconds")
        values.append(duration_seconds)

    cursor = connection.execute(
        f"""
        INSERT INTO lineup_segments ({', '.join(columns)})
        VALUES ({', '.join(['?'] * len(values))})
        """,
        values,
    )
    segment_id = int(cursor.lastrowid)
    lineup_segment_cache[cache_key] = segment_id
    return segment_id, True


def process_game(
    connection: sqlite3.Connection,
    game_row: sqlite3.Row,
    player_lookup: dict[str, str],
    lineup_cache: dict[tuple[str, str], int],
    lineup_segment_cache: dict[str, int],
) -> dict[str, int | str]:
    game_data = fetch_json(game_row["json_url"])
    sorted_events = extract_sorted_events(game_data)
    period_events = group_period_events(sorted_events)

    home_team_id = normalize_text(game_row["home_team_id"])
    away_team_id = normalize_text(game_row["away_team_id"])
    if not home_team_id or not away_team_id:
        raise ValueError(f"Missing team ids for game {game_row['game_id']}")

    team_ids = {"1": home_team_id, "2": away_team_id}
    team_labels = {"1": "home", "2": "away"}

    current_lineups: dict[str, set[str]] = {
        team_number: set(build_team_roster(game_data, team_number, connection, player_lookup))
        for team_number in ("1", "2")
    }

    lineups_created = 0
    segments_created = 0

    for period in sorted(period_events):
        events_in_period = period_events[period]
        period_start_clock = normalize_text(events_in_period[0][1].get("clock")) or "10:00:00"
        period_end_clock = normalize_text(events_in_period[-1][1].get("clock")) or "00:00:00"
        period_start_action_number = int(events_in_period[0][1].get("actionNumber") or events_in_period[0][0])
        period_end_action_number = int(events_in_period[-1][1].get("actionNumber") or events_in_period[-1][0])

        for team_number in ("1", "2"):
            team_id = team_ids[team_number]
            team_label = team_labels[team_number]
            team_substitutions = prepare_team_substitutions(events_in_period, team_number, connection, player_lookup)
            grouped_substitutions = group_substitutions_by_batch(team_substitutions)

            start_groups = [group for group in grouped_substitutions if group[0]["clock"] == period_start_clock]
            middle_groups = [
                group
                for group in grouped_substitutions
                if group[0]["clock"] not in {period_start_clock, period_end_clock}
            ]
            end_groups = [group for group in grouped_substitutions if group[0]["clock"] == period_end_clock]

            lineup = set(current_lineups[team_number])
            consumed_middle_groups = 0
            consumed_end_groups = 0
            if start_groups:
                start_substitutions = [substitution for group in start_groups for substitution in group]
                try:
                    lineup = apply_substitution_batch(
                        lineup,
                        start_substitutions,
                        team_label,
                        period,
                        period_start_clock,
                    )
                except ValueError as exc:
                    if "Invalid lineup size 4" not in str(exc):
                        raise

                    lineup_before_start = sorted(lineup, key=natural_player_sort_key)
                    lineup_after_remove_set = apply_substitution_batch(
                        set(lineup),
                        start_substitutions,
                        team_label,
                        period,
                        period_start_clock,
                        validate_size=False,
                    )
                    if len(lineup_after_remove_set) != 4:
                        raise

                    start_action_numbers = [substitution["actionNumber"] for substitution in start_substitutions]
                    next_incoming_action = find_next_incoming_before_resume(
                        events_in_period,
                        team_number,
                        max(start_action_numbers),
                    )
                    if next_incoming_action is None:
                        raise

                    involved_action_numbers = list(start_action_numbers)
                    lineup_after_add_set = set(lineup_after_remove_set)
                    resolved = False
                    tail_groups = list(middle_groups) + list(end_groups)
                    consume_index = 0

                    while consume_index < len(tail_groups):
                        deferred_group = tail_groups[consume_index]
                        deferred_action_numbers = [substitution["actionNumber"] for substitution in deferred_group]
                        lineup_after_add_set = apply_substitution_batch(
                            lineup_after_add_set,
                            deferred_group,
                            team_label,
                            period,
                            deferred_group[0]["clock"],
                            validate_size=False,
                        )
                        involved_action_numbers.extend(deferred_action_numbers)

                        if next_incoming_action in deferred_action_numbers and len(lineup_after_add_set) == 5:
                            resolved = True
                            break

                        consume_index += 1

                    if not resolved:
                        raise

                    print("DEFERRED_VALIDATION")
                    print(f"game_id={game_row['game_id']}")
                    print(f"period={period}")
                    print(f"clock={period_start_clock}")
                    print(f"action_numbers_involved={','.join(str(value) for value in sorted(set(involved_action_numbers)))}")
                    print(f"lineup_before={','.join(lineup_before_start)}")
                    print(f"lineup_after_remove={','.join(sorted(lineup_after_remove_set, key=natural_player_sort_key))}")
                    print(f"lineup_after_add={','.join(sorted(lineup_after_add_set, key=natural_player_sort_key))}")
                    print("defer_reason=incoming substitution exists for same team before play resumes")

                    lineup = set(lineup_after_add_set)
                    groups_consumed = consume_index + 1
                    consumed_middle_groups = min(groups_consumed, len(middle_groups))
                    consumed_end_groups = max(0, groups_consumed - len(middle_groups))

            current_segment_start_clock = period_start_clock
            current_segment_start_action_number = (
                max(substitution["actionNumber"] for group in start_groups for substitution in group)
                if start_groups
                else period_start_action_number
            )

            middle_index = consumed_middle_groups
            while middle_index < len(middle_groups):
                group = middle_groups[middle_index]
                group_clock = group[0]["clock"]
                group_action_numbers = [substitution["actionNumber"] for substitution in group]

                lineup_id, created = get_or_create_lineup(
                    connection,
                    lineup_cache,
                    team_id,
                    sorted(lineup, key=natural_player_sort_key),
                )
                if created:
                    lineups_created += 1

                _, created_segment = insert_segment(
                    connection,
                    lineup_segment_cache,
                    lineup_id,
                    game_row["game_id"],
                    team_id,
                    period,
                    current_segment_start_clock,
                    group_clock,
                    current_segment_start_action_number,
                    min(group_action_numbers),
                )
                if created_segment:
                    segments_created += 1

                try:
                    lineup = apply_substitution_batch(lineup, group, team_label, period, group_clock)
                    current_segment_start_clock = group_clock
                    current_segment_start_action_number = max(group_action_numbers)
                    middle_index += 1
                    continue
                except ValueError as exc:
                    if "Invalid lineup size 4" not in str(exc):
                        raise

                    lineup_before = sorted(lineup, key=natural_player_sort_key)
                    lineup_after_remove_set = apply_substitution_batch(
                        set(lineup),
                        group,
                        team_label,
                        period,
                        group_clock,
                        validate_size=False,
                    )

                    if len(lineup_after_remove_set) != 4:
                        raise

                    next_incoming_action = find_next_incoming_before_resume(
                        events_in_period,
                        team_number,
                        max(group_action_numbers),
                    )
                    if next_incoming_action is None:
                        raise

                    involved_action_numbers = list(group_action_numbers)
                    lineup_after_add_set = set(lineup_after_remove_set)
                    resolved = False
                    remaining_middle_groups = middle_groups[middle_index + 1 :]
                    tail_groups = list(remaining_middle_groups) + list(end_groups)
                    consume_index = 0

                    while consume_index < len(tail_groups):
                        deferred_group = tail_groups[consume_index]
                        deferred_action_numbers = [substitution["actionNumber"] for substitution in deferred_group]
                        lineup_after_add_set = apply_substitution_batch(
                            lineup_after_add_set,
                            deferred_group,
                            team_label,
                            period,
                            deferred_group[0]["clock"],
                            validate_size=False,
                        )
                        involved_action_numbers.extend(deferred_action_numbers)

                        if next_incoming_action in deferred_action_numbers and len(lineup_after_add_set) == 5:
                            resolved = True
                            break

                        consume_index += 1

                    if not resolved:
                        raise

                    print("DEFERRED_VALIDATION")
                    print(f"game_id={game_row['game_id']}")
                    print(f"period={period}")
                    print(f"clock={group_clock}")
                    print(f"action_numbers_involved={','.join(str(value) for value in sorted(set(involved_action_numbers)))}")
                    print(f"lineup_before={','.join(lineup_before)}")
                    print(f"lineup_after_remove={','.join(sorted(lineup_after_remove_set, key=natural_player_sort_key))}")
                    print(f"lineup_after_add={','.join(sorted(lineup_after_add_set, key=natural_player_sort_key))}")
                    print("defer_reason=incoming substitution exists for same team before play resumes")

                    lineup = set(lineup_after_add_set)
                    resolved_group = tail_groups[consume_index]
                    current_segment_start_clock = resolved_group[0]["clock"]
                    current_segment_start_action_number = max(involved_action_numbers)
                    groups_consumed = consume_index + 1
                    consumed_from_middle_tail = min(groups_consumed, len(remaining_middle_groups))
                    consumed_from_end = max(0, groups_consumed - len(remaining_middle_groups))
                    consumed_end_groups = max(consumed_end_groups, consumed_from_end)
                    middle_index = middle_index + 1 + consumed_from_middle_tail

            lineup_id, created = get_or_create_lineup(
                connection,
                lineup_cache,
                team_id,
                sorted(lineup, key=natural_player_sort_key),
            )
            if created:
                lineups_created += 1

            _, created_segment = insert_segment(
                connection,
                lineup_segment_cache,
                lineup_id,
                game_row["game_id"],
                team_id,
                period,
                current_segment_start_clock,
                period_end_clock,
                current_segment_start_action_number,
                period_end_action_number,
            )
            if created_segment:
                segments_created += 1

            remaining_end_groups = end_groups[consumed_end_groups:]
            if remaining_end_groups:
                lineup = apply_substitution_batch(
                    lineup,
                    [substitution for group in remaining_end_groups for substitution in group],
                    team_label,
                    period,
                    period_end_clock,
                )

            current_lineups[team_number] = lineup

    return {
        "game_id": game_row["game_id"],
        "segments_created": segments_created,
        "lineups_created": lineups_created,
    }


def validate_one_game(
    connection: sqlite3.Connection,
    game_row: sqlite3.Row,
    player_lookup: dict[str, str],
    lineup_cache: dict[tuple[str, str], int],
    lineup_segment_cache: dict[str, int],
) -> bool:
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = process_game(
            connection,
            game_row,
            player_lookup,
            lineup_cache,
            lineup_segment_cache,
        )
        connection.rollback()
        print(f"game_id={result['game_id']}")
        print(f"segments_created={result['segments_created']}")
        print(f"unique_lineups_created={result['lineups_created']}")
        print("validation=passed")
        return True
    except Exception:
        connection.rollback()
        print(f"game_id={game_row['game_id']}")
        print("segments_created=0")
        print("unique_lineups_created=0")
        print("validation=failed")
        return False


def process_all_games(connection: sqlite3.Connection) -> dict[str, int]:
    player_lookup = build_player_lookup(connection)
    lineup_cache: dict[tuple[str, str], int] = {}
    for row in connection.execute("SELECT lineup_id, team_id, lineup_hash FROM lineups"):
        lineup_cache[(row[1], row[2])] = int(row[0])

    lineup_segment_cache: dict[str, int] = {}
    pending_games = load_game_rows(connection)

    games_processed = 0
    games_succeeded = 0
    games_failed = 0
    lineups_created = 0
    lineup_segments_created = 0
    runtime_errors = 0

    for game_row in pending_games:
        games_processed += 1
        lineup_cache_before = dict(lineup_cache)
        lineup_segment_cache_before = dict(lineup_segment_cache)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = process_game(connection, game_row, player_lookup, lineup_cache, lineup_segment_cache)
            connection.commit()
            games_succeeded += 1
            lineups_created += int(result["lineups_created"])
            lineup_segments_created += int(result["segments_created"])
        except Exception:
            connection.rollback()
            lineup_cache.clear()
            lineup_cache.update(lineup_cache_before)
            lineup_segment_cache.clear()
            lineup_segment_cache.update(lineup_segment_cache_before)
            games_failed += 1
            runtime_errors += 1

    return {
        "games_processed": games_processed,
        "games_succeeded": games_succeeded,
        "games_failed": games_failed,
        "lineups_created": lineups_created,
        "lineup_segments_created": lineup_segments_created,
        "runtime_errors": runtime_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct lineups and lineup segments from LiveStats events")
    parser.add_argument("--database", default=str(DATABASE_PATH), help="SQLite database path")
    parser.add_argument("--debug", action="store_true", help="Run debug analysis for first invalid lineup state per failed game")
    parser.add_argument("--trace", action="store_true", help="Run single-game trace mode for lineup transitions")
    parser.add_argument("--trace-game", default="1_20222023_0008", help="Game id for --trace mode")
    args = parser.parse_args()

    database_path = Path(args.database)

    if args.trace:
        trace_connection = connect(database_path)
        try:
            run_trace_mode(trace_connection, args.trace_game)
        finally:
            trace_connection.close()
        return

    if args.debug:
        debug_connection = connect(database_path)
        try:
            run_debug_mode(debug_connection)
        finally:
            debug_connection.close()
        return

    validation_connection = connect(database_path)
    try:
        ensure_duration_seconds_column(validation_connection)
        validation_connection.commit()

        validation_game = load_validation_game(validation_connection)
        if validation_game is None:
            print("game_id=none")
            print("segments_created=0")
            print("unique_lineups_created=0")
            print("validation=passed")
            print("games_processed=0")
            print("games_succeeded=0")
            print("games_failed=0")
            print("lineups_created=0")
            print("lineup_segments_created=0")
            print("runtime_errors=0")
            return

        validation_player_lookup = build_player_lookup(validation_connection)
        validation_lineup_cache: dict[tuple[str, str], int] = {}
        validation_segment_cache: dict[str, int] = {}

        while validation_game is not None:
            if validate_one_game(
                validation_connection,
                validation_game,
                validation_player_lookup,
                validation_lineup_cache,
                validation_segment_cache,
            ):
                break
            validation_game = validation_connection.execute(
                """
                SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url
                FROM games g
                JOIN source_livestats_games s ON s.game_id = g.game_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM lineup_segments ls
                    WHERE ls.game_id = g.game_id
                )
                  AND g.game_id > ?
                ORDER BY g.game_id
                LIMIT 1
                """,
                (validation_game["game_id"],),
            ).fetchone()

        if validation_game is None:
            print("game_id=none")
            print("segments_created=0")
            print("unique_lineups_created=0")
            print("validation=failed")
    finally:
        validation_connection.close()

    processing_connection = connect(database_path)
    try:
        ensure_duration_seconds_column(processing_connection)
        processing_connection.commit()
        summary = process_all_games(processing_connection)
        print(f"games_processed={summary['games_processed']}")
        print(f"games_succeeded={summary['games_succeeded']}")
        print(f"games_failed={summary['games_failed']}")
        print(f"lineups_created={summary['lineups_created']}")
        print(f"lineup_segments_created={summary['lineup_segments_created']}")
        print(f"runtime_errors={summary['runtime_errors']}")
    finally:
        processing_connection.close()


if __name__ == "__main__":
    main()