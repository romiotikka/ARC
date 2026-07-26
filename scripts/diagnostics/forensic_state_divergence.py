from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lineup_segment_generator import (
    DATABASE_PATH,
    FAILURE_IN_PLAYER_ALREADY_PRESENT,
    FAILURE_INVALID_SIZE,
    FAILURE_OUT_PLAYER_NOT_PRESENT,
    FAILURE_PERIOD_TRANSITION_ERROR,
    FAILURE_SUBSTITUTION_BATCH_ERROR,
    FAILURE_UNKNOWN,
    connect,
    extract_sorted_events,
    fetch_json,
    group_period_events,
    load_game_rows,
    normalize_text,
    resolve_player_id,
    build_player_lookup,
    build_team_roster,
    natural_player_sort_key,
    validate_lineup_state,
)


def format_lineup(lineup: set[str]) -> str:
    return ",".join(sorted(lineup, key=natural_player_sort_key))


def resolve_team_players(game_data: dict, team_number: str, connection, player_lookup: dict[str, str]) -> set[str]:
    resolved: set[str] = set()
    team_payload = game_data.get("tm", {}).get(team_number) or {}
    for player in (team_payload.get("pl") or {}).values():
        player_name = normalize_text(player.get("name") or player.get("scoreboardName"))
        if not player_name:
            continue
        try:
            resolved.add(resolve_player_id(connection, player_lookup, player_name))
        except Exception:
            continue
    return resolved


def impossible_explanation(reason_code: str, mutation: str) -> str:
    if reason_code == FAILURE_IN_PLAYER_ALREADY_PRESENT:
        return "player entered twice"
    if reason_code == FAILURE_OUT_PLAYER_NOT_PRESENT:
        return "player left twice or was removed before this event"
    if reason_code == FAILURE_SUBSTITUTION_BATCH_ERROR:
        return "same-clock substitution sequence produced impossible membership"
    if reason_code == FAILURE_PERIOD_TRANSITION_ERROR:
        return "player remained across periods incorrectly"
    if reason_code == FAILURE_INVALID_SIZE:
        if mutation.startswith("REMOVE("):
            return "player was removed at wrong event (lineup dropped below 5)"
        if mutation.startswith("ADD("):
            return "player was added at wrong event (lineup exceeded 5)"
        return "lineup size became impossible"
    if reason_code == FAILURE_UNKNOWN:
        return "unknown impossible transition"
    return f"impossible transition: {reason_code}"


def analyze_game(connection, game_row, player_lookup: dict[str, str]) -> dict:
    game_id = game_row["game_id"]
    game_data = fetch_json(game_row["json_url"])
    sorted_events = extract_sorted_events(game_data)

    team_ids = {
        "1": normalize_text(game_row["home_team_id"]) or "",
        "2": normalize_text(game_row["away_team_id"]) or "",
    }

    allowed_players = {
        team_ids["1"]: resolve_team_players(game_data, "1", connection, player_lookup),
        team_ids["2"]: resolve_team_players(game_data, "2", connection, player_lookup),
    }

    starters = {
        team_ids["1"]: set(build_team_roster(game_data, "1", connection, player_lookup)),
        team_ids["2"]: set(build_team_roster(game_data, "2", connection, player_lookup)),
    }

    current_lineups = {
        team_ids["1"]: set(starters[team_ids["1"]]),
        team_ids["2"]: set(starters[team_ids["2"]]),
    }

    roster_by_team = {
        team_ids["1"]: set(allowed_players[team_ids["1"]]),
        team_ids["2"]: set(allowed_players[team_ids["2"]]),
    }

    entry_history = {
        team_ids["1"]: defaultdict(list),
        team_ids["2"]: defaultdict(list),
    }
    exit_history = {
        team_ids["1"]: defaultdict(list),
        team_ids["2"]: defaultdict(list),
    }

    period_events = group_period_events(sorted_events)
    period_start_lineups = {
        team_ids["1"]: {},
        team_ids["2"]: {},
    }
    for period in sorted(period_events):
        period_start_lineups[team_ids["1"]][period] = None
        period_start_lineups[team_ids["2"]][period] = None

    timeline = []
    first_invalid = None
    last_period_seen = 0

    for pos, (index, event) in enumerate(sorted_events):
        period = int(event.get("period") or 0)
        if period != last_period_seen:
            for team_id in (team_ids["1"], team_ids["2"]):
                if period_start_lineups[team_id].get(period) is None:
                    period_start_lineups[team_id][period] = set(current_lineups[team_id])
            last_period_seen = period

        action_number = int(event.get("actionNumber") or index)
        clock = normalize_text(event.get("clock")) or ""
        event_type = (normalize_text(event.get("actionType")) or "").lower()
        team_number = normalize_text(event.get("tno")) or ""
        team_id = team_ids.get(team_number) or ""
        sub_type = (normalize_text(event.get("subType")) or "").lower()

        lineup_before = ""
        lineup_after = ""
        mutation = "NO_CHANGE"

        reason_code = None
        reason_message = ""
        failing_team_id = team_id if team_id in current_lineups else None

        if team_id in current_lineups:
            lineup_before = format_lineup(current_lineups[team_id])

        if event_type == "substitution" and team_id in current_lineups:
            player_name = normalize_text(event.get("player")) or ""
            try:
                player_id = resolve_player_id(connection, player_lookup, player_name)
            except Exception:
                player_id = None

            if sub_type == "out":
                if not player_id:
                    mutation = f"OUT_UNRESOLVED({player_name})"
                    reason_code = FAILURE_UNKNOWN
                    reason_message = "substitution out could not be resolved"
                elif player_id not in current_lineups[team_id]:
                    mutation = f"OUT_NOT_ON_COURT({player_id})"
                    reason_code = FAILURE_OUT_PLAYER_NOT_PRESENT
                    reason_message = f"out player {player_id} not present"
                else:
                    current_lineups[team_id].remove(player_id)
                    exit_history[team_id][player_id].append(
                        {
                            "action_number": action_number,
                            "period": period,
                            "clock": clock,
                            "period_transition": period > 1 and clock == "10:00:00",
                        }
                    )
                    mutation = f"REMOVE({player_id})"
            elif sub_type == "in":
                if not player_id:
                    mutation = f"IN_UNRESOLVED({player_name})"
                    reason_code = FAILURE_UNKNOWN
                    reason_message = "substitution in could not be resolved"
                elif player_id in current_lineups[team_id]:
                    mutation = f"IN_ALREADY_ON_COURT({player_id})"
                    reason_code = FAILURE_IN_PLAYER_ALREADY_PRESENT
                    reason_message = f"in player {player_id} already present"
                else:
                    current_lineups[team_id].add(player_id)
                    entry_history[team_id][player_id].append(
                        {
                            "action_number": action_number,
                            "period": period,
                            "clock": clock,
                        }
                    )
                    mutation = f"ADD({player_id})"
            else:
                mutation = f"SUBTYPE_MISSING({sub_type})"
                reason_code = FAILURE_UNKNOWN
                reason_message = "missing substitution direction"

        if team_id in current_lineups:
            lineup_after = format_lineup(current_lineups[team_id])

        if reason_code is None:
            for validation_team_id in (team_ids["1"], team_ids["2"]):
                ok, code, reason = validate_lineup_state(
                    list(current_lineups[validation_team_id]),
                    validation_team_id,
                    allowed_players[validation_team_id],
                )
                if not ok:
                    reason_code = code
                    reason_message = reason
                    failing_team_id = validation_team_id
                    if (
                        pos > 0
                        and int(sorted_events[pos - 1][1].get("period") or 0) != period
                        and code in {FAILURE_INVALID_SIZE, "DUPLICATE_PLAYER"}
                    ):
                        reason_code = FAILURE_PERIOD_TRANSITION_ERROR
                    break

        timeline.append(
            {
                "position": pos,
                "index": index,
                "action_number": action_number,
                "period": period,
                "clock": clock,
                "event_type": event_type,
                "team_id": team_id,
                "lineup_before": lineup_before,
                "mutation": mutation,
                "lineup_after": lineup_after,
                "sub_type": sub_type,
            }
        )

        if first_invalid is None and reason_code is not None:
            first_invalid = {
                "position": pos,
                "index": index,
                "action_number": action_number,
                "period": period,
                "clock": clock,
                "event_type": event_type,
                "team_id": failing_team_id,
                "source_team_id": team_id,
                "reason_code": reason_code,
                "reason_message": reason_message,
                "mutation": mutation,
                "lineup_before": lineup_before,
                "lineup_after": lineup_after,
                "impossible_explanation": impossible_explanation(reason_code, mutation),
            }
            break

    if first_invalid is None:
        return {
            "game_id": game_id,
            "status": "no_invalid_state",
        }

    fail_pos = first_invalid["position"]
    start = max(0, fail_pos - 100)
    end = min(len(timeline), fail_pos + 51)

    fail_period = first_invalid["period"]
    fail_clock = first_invalid["clock"]
    fail_team_id = first_invalid["source_team_id"]

    window = []
    for row in timeline[start:end]:
        is_failing_event = (
            row["position"] == fail_pos
            or (
                row["period"] == fail_period
                and row["clock"] == fail_clock
                and row["team_id"] == fail_team_id
                and row["event_type"] == "substitution"
            )
        )
        row_copy = dict(row)
        row_copy["is_failing_event"] = is_failing_event
        window.append(row_copy)

    failing_team = first_invalid["team_id"]
    failure_period = first_invalid["period"]
    on_court = set()
    for row in reversed(window):
        if row["team_id"] == failing_team and row["lineup_after"]:
            on_court = set(player for player in row["lineup_after"].split(",") if player)
            break

    if not on_court:
        on_court = set(player for player in (first_invalid["lineup_after"] or "").split(",") if player)

    if not on_court and failing_team in current_lineups:
        on_court = set(current_lineups[failing_team])

    period_start_lineup = period_start_lineups[failing_team].get(failure_period) or set()

    on_court_reasons = []
    for player_id in sorted(on_court, key=natural_player_sort_key):
        entries = entry_history[failing_team][player_id]
        if failure_period > 1 and player_id in period_start_lineup and (not entries or entries[-1]["period"] < failure_period):
            reason = "remained from previous period"
        elif entries:
            reason = f"entered at action {entries[-1]['action_number']}"
        else:
            reason = "starter"
        on_court_reasons.append({"player_id": player_id, "reason": reason})

    all_players = set(roster_by_team[failing_team])
    off_court = sorted(all_players - on_court, key=natural_player_sort_key)
    off_court_reasons = []
    for player_id in off_court:
        exits = exit_history[failing_team][player_id]
        entries = entry_history[failing_team][player_id]
        if exits:
            last_exit = exits[-1]
            if last_exit["period_transition"]:
                reason = f"removed at period transition (action {last_exit['action_number']})"
            else:
                reason = f"substituted out at action {last_exit['action_number']}"
        elif not entries and player_id not in starters[failing_team]:
            reason = "never entered"
        else:
            reason = "never entered"
        off_court_reasons.append({"player_id": player_id, "reason": reason})

    signature = (
        f"reason={first_invalid['reason_code']}|event_type={first_invalid['event_type']}|"
        f"mutation={first_invalid['mutation']}|lineup_before={len([p for p in first_invalid['lineup_before'].split(',') if p])}|"
        f"lineup_after={len([p for p in first_invalid['lineup_after'].split(',') if p])}"
    )

    return {
        "game_id": game_id,
        "status": "failed",
        "first_invalid_transition": first_invalid,
        "first_impossible_action_signature": signature,
        "timeline_window": window,
        "players_on_court": on_court_reasons,
        "players_not_on_court": off_court_reasons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Forensic state divergence analyzer for failed lineup reconstruction games")
    parser.add_argument("--database", default=str(DATABASE_PATH), help="SQLite database path")
    parser.add_argument(
        "--output",
        default="logs/forensic_state_divergence_report.json",
        help="Output JSON report path",
    )
    args = parser.parse_args()

    database_path = Path(args.database)
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path(__file__).resolve().parents[1] / output_path

    connection = connect(database_path)
    try:
        pending_games = load_game_rows(connection)
        player_lookup = build_player_lookup(connection)

        games = []
        failures = []

        for game_row in pending_games:
            try:
                analysis = analyze_game(connection, game_row, player_lookup)
            except Exception as exc:
                analysis = {
                    "game_id": game_row["game_id"],
                    "status": "error",
                    "error": str(exc),
                }
            games.append(analysis)
            if analysis.get("status") == "failed":
                failures.append(analysis)

        grouped = Counter(item["first_impossible_action_signature"] for item in failures)
        grouped_details = []
        for signature, count in grouped.most_common():
            sample_games = [item["game_id"] for item in failures if item["first_impossible_action_signature"] == signature][:10]
            grouped_details.append(
                {
                    "signature": signature,
                    "count": count,
                    "sample_game_ids": sample_games,
                }
            )

        report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "games_processed": len(games),
            "games_failed": len(failures),
            "groups": grouped_details,
            "games": games,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

        print(f"report_path={output_path}")
        print(f"games_processed={len(games)}")
        print(f"games_failed={len(failures)}")
        for group in grouped_details:
            print(f"signature={group['signature']}")
            print(f"count={group['count']}")
            print(f"sample_game_ids={','.join(group['sample_game_ids'])}")

    finally:
        connection.close()


if __name__ == "__main__":
    main()
