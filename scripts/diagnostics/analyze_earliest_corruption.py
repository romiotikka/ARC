from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lineup_segment_generator import (
    DATABASE_PATH,
    DEADBALL_EVENT_TYPES,
    build_player_lookup,
    build_team_roster,
    connect,
    extract_sorted_events,
    fetch_json,
    load_game_row_by_id,
    natural_player_sort_key,
    normalize_text,
    resolve_player_id,
)


IMPLAUSIBLE_MUTATIONS = {
    "IN_ALREADY_ON_COURT",
    "OUT_NOT_ON_COURT",
    "IN_UNRESOLVED",
    "OUT_UNRESOLVED",
    "SUBTYPE_MISSING",
}


def parse_mutation_label(mutation: str) -> tuple[str, str | None]:
    if "(" not in mutation or not mutation.endswith(")"):
        return mutation, None
    base = mutation.split("(", 1)[0]
    value = mutation.split("(", 1)[1][:-1]
    return base, value


def format_lineup(players: set[str]) -> str:
    return ",".join(sorted(players, key=natural_player_sort_key))


def resolve_player_id_safe(connection, player_lookup: dict[str, str], player_name: str | None) -> str | None:
    if not normalize_text(player_name):
        return None
    try:
        return resolve_player_id(connection, player_lookup, normalize_text(player_name) or "")
    except Exception:
        return None


def build_event_records_for_game(connection, game_row, player_lookup: dict[str, str]) -> tuple[list[dict], dict[str, dict[str, dict]]]:
    game_data = fetch_json(game_row["json_url"])
    sorted_events = extract_sorted_events(game_data)

    team_ids = {
        "1": normalize_text(game_row["home_team_id"]) or "",
        "2": normalize_text(game_row["away_team_id"]) or "",
    }

    starters = {
        team_ids["1"]: set(build_team_roster(game_data, "1", connection, player_lookup)),
        team_ids["2"]: set(build_team_roster(game_data, "2", connection, player_lookup)),
    }

    current = {
        team_ids["1"]: set(starters[team_ids["1"]]),
        team_ids["2"]: set(starters[team_ids["2"]]),
    }

    player_histories: dict[str, dict[str, dict]] = {
        team_ids["1"]: defaultdict(lambda: {"entries": [], "exits": [], "period_transitions": []}),
        team_ids["2"]: defaultdict(lambda: {"entries": [], "exits": [], "period_transitions": []}),
    }

    for team_id in (team_ids["1"], team_ids["2"]):
        for player_id in starters[team_id]:
            player_histories[team_id][player_id]["entries"].append(
                {"action_number": None, "period": 0, "clock": "", "reason": "starter"}
            )

    records: list[dict] = []
    current_period = None

    for position, (index, event) in enumerate(sorted_events):
        period = int(event.get("period") or 0)
        action_number = int(event.get("actionNumber") or index)
        clock = normalize_text(event.get("clock")) or ""
        event_type = (normalize_text(event.get("actionType")) or "").lower()
        team_number = normalize_text(event.get("tno")) or ""
        team_id = team_ids.get(team_number) or ""
        sub_type = (normalize_text(event.get("subType")) or "").lower()

        if current_period is None:
            current_period = period
        elif period != current_period:
            for transition_team_id in (team_ids["1"], team_ids["2"]):
                for player_id in sorted(current[transition_team_id], key=natural_player_sort_key):
                    player_histories[transition_team_id][player_id]["period_transitions"].append(
                        {
                            "from_period": current_period,
                            "to_period": period,
                            "action_number": action_number,
                            "clock": clock,
                        }
                    )
            current_period = period

        lineup_before = ""
        lineup_after = ""
        mutation = "NO_CHANGE"

        if team_id in current:
            lineup_before = format_lineup(current[team_id])

        if event_type == "substitution" and team_id in current:
            player_name = normalize_text(event.get("player"))
            player_id = resolve_player_id_safe(connection, player_lookup, player_name)

            if sub_type == "out":
                if player_id is None:
                    mutation = f"OUT_UNRESOLVED({player_name or ''})"
                elif player_id not in current[team_id]:
                    mutation = f"OUT_NOT_ON_COURT({player_id})"
                else:
                    current[team_id].remove(player_id)
                    player_histories[team_id][player_id]["exits"].append(
                        {"action_number": action_number, "period": period, "clock": clock}
                    )
                    mutation = f"REMOVE({player_id})"
            elif sub_type == "in":
                if player_id is None:
                    mutation = f"IN_UNRESOLVED({player_name or ''})"
                elif player_id in current[team_id]:
                    mutation = f"IN_ALREADY_ON_COURT({player_id})"
                else:
                    current[team_id].add(player_id)
                    player_histories[team_id][player_id]["entries"].append(
                        {"action_number": action_number, "period": period, "clock": clock, "reason": "substitution_in"}
                    )
                    mutation = f"ADD({player_id})"
            else:
                mutation = f"SUBTYPE_MISSING({sub_type})"

        if team_id in current:
            lineup_after = format_lineup(current[team_id])

        records.append(
            {
                "position": position,
                "index": index,
                "action_number": action_number,
                "period": period,
                "clock": clock,
                "event_type": event_type,
                "team_id": team_id,
                "lineup_before": lineup_before,
                "mutation": mutation,
                "lineup_after": lineup_after,
                "raw_sub_type": sub_type,
            }
        )

    return records, player_histories


def lineupsize(text: str) -> int:
    return len([item for item in text.split(",") if item])


def find_deadball_block(records: list[dict], failure_position: int) -> tuple[int, int]:
    start = failure_position
    while start > 0:
        prev = records[start - 1]
        if prev["event_type"] == "substitution" or prev["event_type"] in DEADBALL_EVENT_TYPES:
            start -= 1
            continue
        break

    end = failure_position
    while end + 1 < len(records):
        nxt = records[end + 1]
        if nxt["event_type"] == "substitution" or nxt["event_type"] in DEADBALL_EVENT_TYPES:
            end += 1
            continue
        break

    return start, end


def find_earliest_corruption(records: list[dict], first_invalid: dict) -> dict:
    failure_position = int(first_invalid["position"])
    source_team_id = first_invalid.get("source_team_id") or first_invalid.get("team_id")
    fallback = {
        "game_id": None,
        "earliest_corruption_action": int(first_invalid.get("action_number") or 0),
        "clock": first_invalid.get("clock") or "",
        "event_type": first_invalid.get("event_type") or "",
        "team_id": source_team_id,
        "lineup_before": first_invalid.get("lineup_before") or "",
        "mutation": first_invalid.get("mutation") or "",
        "lineup_after": first_invalid.get("lineup_after") or "",
        "corrupted_players": [],
        "reason": "first action where tracked lineup became impossible",
    }

    fallback_base, fallback_value = parse_mutation_label(fallback["mutation"])
    if fallback_value and fallback_base in {"REMOVE", "ADD", *IMPLAUSIBLE_MUTATIONS}:
        fallback["corrupted_players"] = [fallback_value]

    block_start, block_end = find_deadball_block(records, failure_position)
    team_subs = [
        r
        for r in records[block_start : block_end + 1]
        if r["team_id"] == source_team_id and r["event_type"] == "substitution"
    ]

    if not team_subs:
        return fallback

    pre_failure_subs = [r for r in team_subs if int(r["action_number"]) <= int(first_invalid.get("action_number") or 0)]
    if not pre_failure_subs:
        pre_failure_subs = team_subs

    for row in pre_failure_subs:
        mutation_base, mutation_value = parse_mutation_label(row["mutation"])
        if mutation_base in IMPLAUSIBLE_MUTATIONS:
            return {
                "game_id": None,
                "earliest_corruption_action": row["action_number"],
                "clock": row["clock"],
                "event_type": row["event_type"],
                "team_id": row["team_id"],
                "lineup_before": row["lineup_before"],
                "mutation": row["mutation"],
                "lineup_after": row["lineup_after"],
                "corrupted_players": [mutation_value] if mutation_value else [],
                "reason": "substitution mutation was impossible immediately",
            }

    return fallback


def trace_player_before_failure(player_id: str, team_id: str, first_invalid_action: int, first_invalid_period: int, player_history: dict, lineup_before_set: set[str]) -> dict:
    entries = [e for e in player_history.get("entries", []) if (e.get("action_number") is None or e.get("action_number") <= first_invalid_action)]
    exits = [e for e in player_history.get("exits", []) if e.get("action_number") is not None and e.get("action_number") <= first_invalid_action]
    transitions = [t for t in player_history.get("period_transitions", []) if t.get("to_period", 0) <= first_invalid_period]

    last_entry = entries[-1] if entries else None
    last_exit = exits[-1] if exits else None

    if player_id not in lineup_before_set:
        why = "not in lineup_before"
    elif last_entry and last_entry.get("reason") == "starter" and not transitions:
        why = "starter"
    elif last_entry and last_entry.get("action_number") is not None:
        why = f"entered at action {last_entry['action_number']}"
    elif transitions:
        why = "remained from previous period"
    else:
        why = "starter"

    return {
        "player_id": player_id,
        "why_currently_on_court": why,
        "entered_action": last_entry.get("action_number") if last_entry else None,
        "removed_action": last_exit.get("action_number") if last_exit else None,
        "entry_history": entries,
        "exit_history": exits,
        "period_transitions": transitions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze earliest unrecoverable corruption from forensic_state_divergence_report")
    parser.add_argument("--database", default=str(DATABASE_PATH), help="SQLite database path")
    parser.add_argument(
        "--input",
        default="logs/forensic_state_divergence_report.json",
        help="Input forensic report path",
    )
    parser.add_argument(
        "--output",
        default="logs/earliest_corruption_report.json",
        help="Output earliest corruption analysis path",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.is_absolute():
        input_path = root / input_path
    if not output_path.is_absolute():
        output_path = root / output_path

    forensic = json.loads(input_path.read_text(encoding="utf-8"))

    connection = connect(Path(args.database))
    try:
        player_lookup = build_player_lookup(connection)

        failed_games = [g for g in forensic.get("games", []) if g.get("status") == "failed"]
        analyses = []

        for item in failed_games:
            game_id = item["game_id"]
            game_row = load_game_row_by_id(connection, game_id)
            if game_row is None:
                analyses.append({
                    "game_id": game_id,
                    "status": "error",
                    "error": "game row not found",
                })
                continue

            try:
                records, histories = build_event_records_for_game(connection, game_row, player_lookup)
            except Exception as exc:
                analyses.append({
                    "game_id": game_id,
                    "status": "error",
                    "error": str(exc),
                })
                continue

            first_invalid = item["first_invalid_transition"]
            earliest = find_earliest_corruption(records, first_invalid)
            earliest["game_id"] = game_id

            team_id = first_invalid.get("source_team_id") or first_invalid.get("team_id") or ""
            lineup_before_set = set([p for p in (first_invalid.get("lineup_before") or "").split(",") if p])
            first_invalid_action = int(first_invalid.get("action_number") or 0)
            first_invalid_period = int(first_invalid.get("period") or 0)

            player_traces = []
            for player_id in sorted(lineup_before_set, key=natural_player_sort_key):
                trace = trace_player_before_failure(
                    player_id,
                    team_id,
                    first_invalid_action,
                    first_invalid_period,
                    histories.get(team_id, {}).get(player_id, {}),
                    lineup_before_set,
                )
                player_traces.append(trace)

            signature = (
                f"event={earliest['event_type']}|"
                f"team={earliest['team_id']}|mutation={earliest['mutation']}|"
                f"before={lineupsize(earliest['lineup_before'])}|after={lineupsize(earliest['lineup_after'])}|"
                f"reason={earliest['reason']}"
            )

            analyses.append(
                {
                    "game_id": game_id,
                    "status": "ok",
                    "first_invalid_transition": first_invalid,
                    "lineup_before_player_trace": player_traces,
                    "earliest_corruption_state": earliest,
                    "earliest_corruption_signature": signature,
                }
            )

        ok_games = [g for g in analyses if g.get("status") == "ok"]
        grouped = Counter(g["earliest_corruption_signature"] for g in ok_games)

        groups = []
        for signature, count in grouped.most_common():
            examples = [g["game_id"] for g in ok_games if g["earliest_corruption_signature"] == signature][:10]
            groups.append(
                {
                    "earliest_corruption_signature": signature,
                    "count": count,
                    "example_game_ids": examples,
                }
            )

        report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "input_report": str(input_path),
            "games_in_input_failed": len(failed_games),
            "games_analyzed_ok": len(ok_games),
            "games_analyzed_error": len([g for g in analyses if g.get("status") == "error"]),
            "groups": groups,
            "games": analyses,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

        print(f"output_path={output_path}")
        print(f"games_in_input_failed={len(failed_games)}")
        print(f"games_analyzed_ok={len(ok_games)}")
        print(f"games_analyzed_error={len([g for g in analyses if g.get('status') == 'error'])}")
        for row in groups[:20]:
            print(f"signature={row['earliest_corruption_signature']}")
            print(f"count={row['count']}")
            print(f"example_game_ids={','.join(row['example_game_ids'])}")

    finally:
        connection.close()


if __name__ == "__main__":
    main()
