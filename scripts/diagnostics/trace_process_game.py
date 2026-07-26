from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import sys
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "scripts" / "core"))

import lineup_reconstructor


DEFAULT_DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"


def format_lineup(players: set[str] | list[str]) -> str:
    return ",".join(sorted(players, key=lineup_reconstructor.natural_player_sort_key))


def build_lineup_cache(connection) -> dict[tuple[str, str], int]:
    cache: dict[tuple[str, str], int] = {}
    for row in connection.execute("SELECT lineup_id, team_id, lineup_hash FROM lineups"):
        cache[(row[1], row[2])] = int(row[0])
    return cache


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trace one process_game run without persisting database changes"
    )
    parser.add_argument("--game-id", required=True, help="ARC game id to trace")
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE_PATH),
        help="SQLite database path",
    )
    args = parser.parse_args()

    connection = lineup_reconstructor.connect(Path(args.database))
    try:
        game_row = lineup_reconstructor.load_game_row_by_id(connection, args.game_id)
        if game_row is None:
            print("TRACE_ERROR")
            print(f"message=game not found: {args.game_id}")
            return

        player_lookup = lineup_reconstructor.build_player_lookup(connection)
        lineup_cache = build_lineup_cache(connection)
        lineup_segment_cache: dict[str, int] = {}

        original_build_team_roster = lineup_reconstructor.build_team_roster
        original_prepare_team_substitutions = lineup_reconstructor.prepare_team_substitutions
        original_group_substitutions_by_batch = lineup_reconstructor.group_substitutions_by_batch
        original_apply_substitution_batch = lineup_reconstructor.apply_substitution_batch
        original_find_next_incoming_before_resume = lineup_reconstructor.find_next_incoming_before_resume
        original_get_or_create_lineup = lineup_reconstructor.get_or_create_lineup
        original_insert_segment = lineup_reconstructor.insert_segment
        team_numbers_by_label = {"home": "1", "away": "2"}
        batch_groups: dict[tuple[str, tuple[int, ...]], str] = {}

        def trace_build_team_roster(game_data, team_number, database, lookup):
            lineup = original_build_team_roster(game_data, team_number, database, lookup)
            print(f"TRACE_STARTING_LINEUP team_number={team_number} lineup={format_lineup(lineup)}")
            return lineup

        def trace_prepare_team_substitutions(period_events, team_number, database, lookup):
            substitutions = original_prepare_team_substitutions(
                period_events,
                team_number,
                database,
                lookup,
            )
            batches = original_group_substitutions_by_batch(substitutions)
            period_start_clock = (
                lineup_reconstructor.normalize_text(period_events[0][1].get("clock"))
                or "10:00:00"
            )
            period_end_clock = (
                lineup_reconstructor.normalize_text(period_events[-1][1].get("clock"))
                or "00:00:00"
            )
            raw_events_by_action = {
                int(event.get("actionNumber") or index): event
                for index, event in period_events
            }

            for batch in batches:
                batch_clock = batch[0]["clock"]
                if batch_clock == period_start_clock:
                    group_name = "start_groups"
                elif batch_clock == period_end_clock:
                    group_name = "end_groups"
                else:
                    group_name = "middle_groups"

                action_numbers = tuple(int(item["actionNumber"]) for item in batch)
                batch_groups[(team_number, action_numbers)] = group_name
                print(
                    f"TRACE_BATCH_DISCOVERED group={group_name} team_number={team_number} "
                    f"period={batch[0]['period']} clock={batch_clock} "
                    f"action_numbers={','.join(str(value) for value in action_numbers)}"
                )
                for item in batch:
                    raw_event = raw_events_by_action.get(int(item["actionNumber"]), {})
                    print(
                        "TRACE_BATCH_RAW "
                        f"actionNumber={item['actionNumber']} "
                        f"player_name={raw_event.get('player')} "
                        f"resolved_player_id={item['player_id']} "
                        f"subType={item['subType']} "
                        f"clock={item['clock']} period={item['period']} "
                        f"tno={raw_event.get('tno')}"
                    )
            return substitutions

        def log_reconstructor_warnings(warning_output: io.StringIO) -> None:
            for warning in warning_output.getvalue().splitlines():
                print(f"TRACE_RECONSTRUCTOR_WARNING exact_message={warning}")
                if "ambiguous 1-out/1-in batch as no-op" in warning:
                    print(
                        "TRACE_WARNING_CONDITION "
                        "condition=len(out)==1 and len(in)==1 and out is in current_lineup and in is in current_lineup"
                    )
                elif "ambiguous 1-out/1-in absent-player batch as no-op" in warning:
                    print(
                        "TRACE_WARNING_CONDITION "
                        "condition=len(out)==1 and len(in)==1 and out is not in current_lineup and in is not in current_lineup"
                    )
                elif "ignoring OUT" in warning:
                    print(
                        "TRACE_WARNING_CONDITION "
                        "condition=player_id not in current_lineup when the OUT event is processed"
                    )
                elif "ignoring duplicate IN" in warning:
                    print(
                        "TRACE_WARNING_CONDITION "
                        "condition=player_id already in current_lineup when the IN event is processed"
                    )

        def trace_apply_substitution_batch(current_lineup, substitutions, team_label, period, clock, validate_size=True):
            lineup_before = set(current_lineup)
            ordered_substitutions = sorted(
                substitutions,
                key=lambda substitution: (int(substitution["actionNumber"]), int(substitution["index"])),
            )
            action_number_values = tuple(int(item["actionNumber"]) for item in substitutions)
            action_numbers = ",".join(str(value) for value in action_number_values)
            team_number = team_numbers_by_label[team_label]
            group_name = batch_groups.get((team_number, action_number_values), "unknown")
            trace_name = "TRACE_RECOVERY_STEP" if not validate_size else "TRACE_BATCH_APPLY"
            batch_out_players = [
                lineup_reconstructor.normalize_text(item.get("player_id"))
                for item in ordered_substitutions
                if (lineup_reconstructor.normalize_text(item.get("subType")) or "").lower() == "out"
            ]
            batch_in_players = [
                lineup_reconstructor.normalize_text(item.get("player_id"))
                for item in ordered_substitutions
                if (lineup_reconstructor.normalize_text(item.get("subType")) or "").lower() == "in"
            ]
            ambiguous_present = (
                len(batch_out_players) == 1
                and len(batch_in_players) == 1
                and bool(batch_out_players[0])
                and bool(batch_in_players[0])
                and batch_out_players[0] in lineup_before
                and batch_in_players[0] in lineup_before
            )
            ambiguous_absent = (
                len(batch_out_players) == 1
                and len(batch_in_players) == 1
                and bool(batch_out_players[0])
                and bool(batch_in_players[0])
                and batch_out_players[0] not in lineup_before
                and batch_in_players[0] not in lineup_before
            )
            print(
                f"TRACE_BATCH_BEFORE group={group_name} team={team_label} period={period} clock={clock} "
                f"action_numbers={action_numbers} lineup={format_lineup(lineup_before)}"
            )
            print(
                "TRACE_AMBIGUITY_CHECK "
                f"out_players={batch_out_players} in_players={batch_in_players} "
                f"out_in_lineup={bool(batch_out_players and batch_out_players[0] in lineup_before)} "
                f"in_in_lineup={bool(batch_in_players and batch_in_players[0] in lineup_before)} "
                f"ambiguous_present={ambiguous_present} ambiguous_absent={ambiguous_absent}"
            )
            if ambiguous_present:
                print(
                    "TRACE_NO_OP_CONDITION "
                    "condition=len(out)==1 and len(in)==1 and out is in lineup and in is in lineup"
                )
            if ambiguous_absent:
                print(
                    "TRACE_NO_OP_CONDITION "
                    "condition=len(out)==1 and len(in)==1 and out is not in lineup and in is not in lineup"
                )

            warning_output = io.StringIO()
            try:
                with redirect_stdout(warning_output):
                    lineup_after = original_apply_substitution_batch(
                        current_lineup,
                        substitutions,
                        team_label,
                        period,
                        clock,
                        validate_size=validate_size,
                    )
            except Exception as exc:
                log_reconstructor_warnings(warning_output)
                print(
                    f"{trace_name} group={group_name} team={team_label} period={period} clock={clock} "
                    f"action_numbers={action_numbers} lineup_before={format_lineup(lineup_before)} "
                    f"result=error error={type(exc).__name__}:{exc}"
                )
                if validate_size and "Invalid lineup size 4" in str(exc):
                    print(
                        "TRACE_RECOVERY_ENTER "
                        f"reason={exc} group={group_name} action_numbers={action_numbers}"
                    )
                raise

            log_reconstructor_warnings(warning_output)

            print(
                f"{trace_name} group={group_name} team={team_label} period={period} clock={clock} "
                f"action_numbers={action_numbers} lineup_before={format_lineup(lineup_before)} "
                f"lineup_after={format_lineup(lineup_after)}"
            )
            return lineup_after

        def trace_find_next_incoming_before_resume(events_in_period, team_number, after_action_number):
            incoming_action = original_find_next_incoming_before_resume(
                events_in_period,
                team_number,
                after_action_number,
            )
            events_after_action = [
                f"{int(event.get('actionNumber') or index)}:{event.get('actionType')}:{event.get('tno')}:{event.get('subType')}"
                for index, event in events_in_period
                if int(event.get("actionNumber") or index) > after_action_number
            ]
            print(
                "TRACE_RECOVERY_LOOKAHEAD "
                f"team_number={team_number} after_action_number={after_action_number} "
                f"incoming_action_number={incoming_action} "
                f"events_after_action={'|'.join(events_after_action)}"
            )
            return incoming_action

        def trace_get_or_create_lineup(database, cache, team_id, lineup_players):
            lineup_id, created = original_get_or_create_lineup(database, cache, team_id, lineup_players)
            print(
                f"TRACE_LINEUP team_id={team_id} lineup_id={lineup_id} "
                f"created={created} players={format_lineup(lineup_players)}"
            )
            return lineup_id, created

        def trace_insert_segment(
            database,
            cache,
            lineup_id,
            game_id,
            team_id,
            period,
            start_clock,
            end_clock,
            start_action_number,
            end_action_number,
        ):
            segment_id, created = original_insert_segment(
                database,
                cache,
                lineup_id,
                game_id,
                team_id,
                period,
                start_clock,
                end_clock,
                start_action_number,
                end_action_number,
            )
            print(
                f"TRACE_SEGMENT team_id={team_id} period={period} lineup_id={lineup_id} "
                f"start={start_clock}/{start_action_number} end={end_clock}/{end_action_number} "
                f"created={created}"
            )
            return segment_id, created

        connection.execute("BEGIN IMMEDIATE")
        try:
            with ExitStack() as patches:
                patches.enter_context(
                    patch.object(lineup_reconstructor, "build_team_roster", trace_build_team_roster)
                )
                patches.enter_context(
                    patch.object(
                        lineup_reconstructor,
                        "prepare_team_substitutions",
                        trace_prepare_team_substitutions,
                    )
                )
                patches.enter_context(
                    patch.object(lineup_reconstructor, "apply_substitution_batch", trace_apply_substitution_batch)
                )
                patches.enter_context(
                    patch.object(
                        lineup_reconstructor,
                        "find_next_incoming_before_resume",
                        trace_find_next_incoming_before_resume,
                    )
                )
                patches.enter_context(
                    patch.object(lineup_reconstructor, "get_or_create_lineup", trace_get_or_create_lineup)
                )
                patches.enter_context(
                    patch.object(lineup_reconstructor, "insert_segment", trace_insert_segment)
                )
                result = lineup_reconstructor.process_game(
                    connection,
                    game_row,
                    player_lookup,
                    lineup_cache,
                    lineup_segment_cache,
                )
            print(f"TRACE_RESULT game_id={result['game_id']} segments_created={result['segments_created']} lineups_created={result['lineups_created']}")
        except Exception as exc:
            print(f"TRACE_ERROR type={type(exc).__name__} message={exc}")
            raise
        finally:
            connection.rollback()
            print("TRACE_ROLLBACK complete=true")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
