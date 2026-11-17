from __future__ import annotations

import sqlite3
import json
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts" / "core"))

from lineup_reconstructor import (
    apply_substitution_batch,
    clock_group_key,
    extract_sorted_events,
    get_or_create_lineup,
    group_period_events,
    group_substitutions_by_batch,
    lineup_hash_for_players,
    logical_period_number,
    LineupTransitionError,
    process_game,
    prepare_team_substitutions,
    debug_first_invalid_event,
    build_player_lookup,
    find_next_incoming_before_resume,
    run_trace_mode,
    resolve_substitution_occurrence,
    build_team_roster,
    FAILURE_AMBIGUOUS_PLAYER_IDENTITY,
    FAILURE_PLAYER_IDENTITY_CONFLICT,
    FAILURE_UNRESOLVED_PLAYER_IDENTITY,
    FAILURE_MISSING_STARTER,
)
import lineup_reconstructor as reconstructor


class LineupReconstructorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        schema = (ROOT_DIR / "database" / "schema.sql").read_text()
        self.db.executescript(schema)
        self.db.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'Test League')")
        self.db.execute("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (1, '2022-23', 2022, 2023)")
        self.db.execute("INSERT INTO teams (team_id, canonical_name) VALUES (2, 'Team 2')")
        for pid in range(1, 6):
            self.db.execute(
                "INSERT INTO players (player_id, canonical_name) VALUES (?, ?)",
                (pid, f"Player {pid}"),
            )
        self.db.commit()

    def test_failed_candidate_activation_preserves_active_source_and_reconstruction(self) -> None:
        self.db.row_factory = sqlite3.Row
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Team 3')")
        self.db.execute("INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('atomic-game',1,1,2,3,'boxscore_imported')")
        self.db.execute("INSERT INTO source_livestats_games(game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES('atomic-game',1,1,'provider-atomic','atomic','mock://unused')")
        snapshots = []
        for version in (1, 2):
            snapshot_id = self.db.execute(
                """INSERT INTO source_game_snapshots(game_id,provider,provider_game_id,capture_kind,payload_gzip,captured_body_sha256)
                   VALUES('atomic-game','fiba_livestats',?,'initial_import',?,?)""",
                (str(version), b'raw', str(version) * 64),
            ).lastrowid
            normalization_id = self.db.execute(
                """INSERT INTO source_snapshot_normalizations(snapshot_id,game_id,normalizer_version,normalized_json_gzip,normalized_sha256,status)
                   VALUES(?, 'atomic-game', ?, ?, ?, 'complete')""",
                (snapshot_id, f'v{version}', b'normalized', str(version) * 64),
            ).lastrowid
            decision_set_id = self.db.execute(
                """INSERT INTO identity_decision_sets(normalization_id,decision_set_version,resolver_version,decision_set_sha256,status)
                   VALUES(?,?, 'test', ?, 'complete')""",
                (normalization_id, version, str(version) * 64),
            ).lastrowid
            snapshots.append((snapshot_id, normalization_id, decision_set_id))

        old_snapshot, old_normalization, old_decisions = snapshots[0]
        new_snapshot, _, _ = snapshots[1]
        self.db.execute("INSERT INTO active_game_source_snapshots(game_id,snapshot_id,normalization_id) VALUES('atomic-game',?,?)", (old_snapshot, old_normalization))
        self.db.execute("INSERT INTO active_identity_decision_sets(game_id,normalization_id,decision_set_id) VALUES('atomic-game',?,?)", (old_normalization, old_decisions))
        lineup_id = self.db.execute(
            "INSERT INTO lineups(team_id,player_1,player_2,player_3,player_4,player_5,lineup_hash) VALUES(2,1,2,3,4,5,'atomic-lineup')"
        ).lastrowid
        self.db.execute("INSERT INTO lineup_segments(game_id,lineup_id,team_id,period,start_action_number,end_action_number,start_clock,end_clock,duration_seconds) VALUES('atomic-game',?,2,1,1,2,'10:00','00:00',600)", (lineup_id,))
        old_run = self.db.execute(
            """INSERT INTO reconstruction_runs(game_id,snapshot_id,normalization_id,decision_set_id,algorithm_version,input_sha256,output_sha256,status)
               VALUES('atomic-game',?,?,?,'test',? ,?,'succeeded')""",
            (old_snapshot, old_normalization, old_decisions, 'a' * 64, 'b' * 64),
        ).lastrowid
        self.db.execute("INSERT INTO reconstruction_run_segments(reconstruction_run_id,game_id,lineup_id,team_id,period,start_action_number,end_action_number,start_clock,end_clock,duration_seconds) VALUES(?,'atomic-game',?,2,1,1,2,'10:00','00:00',600)", (old_run, lineup_id))
        self.db.execute("INSERT INTO active_reconstructions(game_id,reconstruction_run_id) VALUES('atomic-game',?)", (old_run,))
        self.db.commit()

        def fail_after_candidate_selection(*args, **kwargs):
            raise ValueError("injected reconstruction failure")

        with patch.object(reconstructor, "_process_game", fail_after_candidate_selection):
            with self.assertRaises(SystemExit):
                reconstructor.run_single_game_mode(self.db, "atomic-game", force=True, source_snapshot_id=int(new_snapshot))

        self.assertEqual(self.db.execute("SELECT snapshot_id FROM active_game_source_snapshots WHERE game_id='atomic-game'").fetchone()[0], old_snapshot)
        self.assertEqual(self.db.execute("SELECT decision_set_id FROM active_identity_decision_sets WHERE game_id='atomic-game'").fetchone()[0], old_decisions)
        self.assertEqual(self.db.execute("SELECT reconstruction_run_id FROM active_reconstructions WHERE game_id='atomic-game'").fetchone()[0], old_run)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM lineup_segments WHERE game_id='atomic-game'").fetchone()[0], 1)
        failed = self.db.execute("SELECT snapshot_id,status FROM reconstruction_runs WHERE game_id='atomic-game' ORDER BY reconstruction_run_id DESC LIMIT 1").fetchone()
        self.assertEqual((failed[0], failed[1]), (new_snapshot, "failed"))

    def tearDown(self) -> None:
        self.db.close()

    def test_reconstruction_input_hash_ignores_identity_evidence_metadata_but_tracks_assignments(self) -> None:
        self.db.row_factory = sqlite3.Row
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Team 3')")
        self.db.execute("INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('hash-game',1,1,2,3,'boxscore_imported')")
        snapshot_id = self.db.execute(
            """INSERT INTO source_game_snapshots(game_id,provider,provider_game_id,capture_kind,payload_gzip,captured_body_sha256)
               VALUES('hash-game','fiba_livestats','hash-game','initial_import',?,?)""",
            (b'raw', 'a' * 64),
        ).lastrowid
        normalization_id = self.db.execute(
            """INSERT INTO source_snapshot_normalizations(snapshot_id,game_id,normalizer_version,normalized_json_gzip,normalized_sha256,status)
               VALUES(?,'hash-game','normalizer-v1',?,?, 'complete')""",
            (snapshot_id, b'normalized', 'b' * 64),
        ).lastrowid
        team_occurrence_id = self.db.execute(
            "INSERT INTO source_team_occurrences(normalization_id,source_ordinal,source_team_key,team_name) VALUES(?,0,'1','Team 2')",
            (normalization_id,),
        ).lastrowid
        occurrence_id = self.db.execute(
            """INSERT INTO source_player_occurrences(normalization_id,team_occurrence_id,identity_occurrence_key,source_ordinal,original_name)
               VALUES(?,?, 'team:1/player:1',0,'P. One')""",
            (normalization_id, team_occurrence_id),
        ).lastrowid

        decision_sets = []
        for version, player_id, evidence in (
            (1, 1, '{"confidence":0.97}'),
            (2, 1, '{"confidence":1.0}'),
            (3, 2, '{"confidence":1.0}'),
        ):
            decision_set_id = self.db.execute(
                """INSERT INTO identity_decision_sets(normalization_id,decision_set_version,resolver_version,decision_set_sha256,status)
                   VALUES(?,?, 'resolver-v1', ?, 'complete')""",
                (normalization_id, version, str(version) * 64),
            ).lastrowid
            self.db.execute(
                """INSERT INTO identity_decisions(decision_set_id,normalization_id,player_occurrence_id,arc_player_id,status,decision_source,evidence_json)
                   VALUES(?,?,?,?,'resolved','resolver',?)""",
                (decision_set_id, normalization_id, occurrence_id, player_id, evidence),
            )
            decision_sets.append(decision_set_id)

        game_row = self.db.execute(
            """SELECT s.captured_body_sha256,n.normalized_sha256,n.normalizer_version,? AS decision_set_id
               FROM source_game_snapshots s JOIN source_snapshot_normalizations n USING(snapshot_id,game_id)
               WHERE s.snapshot_id=?""",
            (decision_sets[0], snapshot_id),
        ).fetchone()
        evidence_only_row = dict(game_row)
        evidence_only_row["decision_set_id"] = decision_sets[1]
        changed_assignment_row = dict(game_row)
        changed_assignment_row["decision_set_id"] = decision_sets[2]

        original_hash = reconstructor._reconstruction_input_sha256(self.db, game_row)
        self.assertEqual(original_hash, reconstructor._reconstruction_input_sha256(self.db, evidence_only_row))
        self.assertNotEqual(original_hash, reconstructor._reconstruction_input_sha256(self.db, changed_assignment_row))
        self.assertEqual(
            reconstructor._identity_interpretation_sha256(self.db, decision_sets[0]),
            reconstructor._identity_interpretation_sha256(self.db, decision_sets[1]),
        )
        self.assertNotEqual(
            reconstructor._identity_interpretation_sha256(self.db, decision_sets[0]),
            reconstructor._identity_interpretation_sha256(self.db, decision_sets[2]),
        )

    def add_lippasaar_occurrences(self) -> dict:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Opponent')")
        self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(125,'Arvan-Johhannes Lippasaar')")
        self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(216,'Arden-Joosep Lippasaar')")
        self.db.execute("INSERT INTO player_aliases(player_id,alias_name,source) VALUES(125,'A. Lippasaar','test')")
        self.db.execute("INSERT INTO player_aliases(player_id,alias_name,source) VALUES(216,'A. Lippasaar','test')")
        self.db.execute("INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('identity-game',1,1,2,3,'boxscore_imported')")
        self.db.execute("INSERT INTO player_games(game_id,player_id,team_id,provider_player_id,player_name,team_name,shirt_number,starter) VALUES('identity-game',125,2,'live-125','A. Lippasaar','Team 2','13',0)")
        self.db.execute("INSERT INTO player_games(game_id,player_id,team_id,provider_player_id,player_name,team_name,shirt_number,starter) VALUES('identity-game',216,2,'live-216','A. Lippasaar','Team 2','1',0)")
        self.db.commit()
        return {"1": {"pl": {
            "roster125": {"name": "A. Lippasaar", "firstName": "Arvan-Johhannes", "familyName": "Lippasaar", "playerId": "live-125", "pno": "pno-125", "shirtNumber": "13"},
            "roster216": {"name": "A. Lippasaar", "firstName": "Arden-Joosep", "familyName": "Lippasaar", "playerId": "live-216", "pno": "pno-216", "shirtNumber": "1"},
        }}}

    def test_null_persisted_id_resolves_lippasaar_through_reconstruction_boundary(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 10, "period": 1, "clock": "05:00:00", "tno": "1", "player": "A. Lippasaar", "playerId": "live-216", "pno": "pno-216", "shirtNumber": "1", "actionType": "substitution", "subType": "in", "success": 1}
        self.db.execute("INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES('identity-game',10,1,'05:00:00',2,NULL,'substitution','success')")
        result = prepare_team_substitutions([(10, event)], "1", self.db, {}, "identity-game", "2", rosters)
        self.assertEqual(result[0]["player_id"], "216")

    def test_null_persisted_id_allows_unique_team_name_only(self) -> None:
        rosters = {"1": {"pl": {"only": {"name": "Unique Player", "firstName": "Unique", "familyName": "Player", "shirtNumber": "8"}}}}
        self.db.execute("INSERT INTO games(game_id,league_id,season_id,home_team_id,status) VALUES('unique-game',1,1,2,'boxscore_imported')")
        self.db.execute("UPDATE players SET canonical_name='Unique Player' WHERE player_id=5")
        self.db.execute("INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('unique-game',5,2,'Unique Player','Team 2','8',0)")
        self.db.execute("INSERT INTO events(game_id,action_number,team_id,player_id) VALUES('unique-game',1,2,NULL)")
        event = {"actionNumber": 1, "tno": "1", "player": "Unique Player", "subType": "in", "actionType": "substitution", "clock": "05:00", "success": 1}
        result = prepare_team_substitutions([(1, event)], "1", self.db, {}, "unique-game", "2", rosters)
        self.assertEqual(result[0]["player_id"], "5")

    def test_null_id_ambiguous_alias_and_no_identifiers_is_not_guessed(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 11, "tno": "1", "player": "A. Lippasaar", "actionType": "substitution", "subType": "in", "clock": "05:00", "success": 1}
        with self.assertRaises(LineupTransitionError) as caught:
            resolve_substitution_occurrence(self.db, "identity-game", "2", event, rosters)
        self.assertEqual(caught.exception.code, FAILURE_AMBIGUOUS_PLAYER_IDENTITY)

    def test_unverified_pno_does_not_disambiguate_alias(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 16, "tno": "1", "player": "A. Lippasaar", "pno": "roster216"}
        with self.assertRaises(LineupTransitionError) as caught:
            resolve_substitution_occurrence(self.db, "identity-game", "2", event, rosters)
        self.assertEqual(caught.exception.code, FAILURE_AMBIGUOUS_PLAYER_IDENTITY)

    def test_non_null_persisted_id_is_validated_and_rejects_wrong_lippasaar(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 17, "period": 1, "clock": "05:00", "tno": "1", "player": "A. Lippasaar", "playerId": "live-216", "shirtNumber": "1", "actionType": "substitution", "subType": "in", "success": 1}
        self.db.execute("INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES('identity-game',16,1,'05:00',2,216,'substitution','success')")
        correct = prepare_team_substitutions([(16, {**event, "actionNumber": 16})], "1", self.db, {}, "identity-game", "2", rosters)
        self.assertEqual(correct[0]["player_id"], "216")
        self.db.execute("INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES('identity-game',17,1,'05:00',2,125,'substitution','success')")
        with self.assertRaises(LineupTransitionError) as caught:
            prepare_team_substitutions([(17, event)], "1", self.db, {}, "identity-game", "2", rosters)
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)

    def test_null_event_uses_persisted_provider_evidence_snapshot(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 19, "period": 1, "clock": "05:00", "tno": "1", "player": "A. Lippasaar", "playerId": "live-125", "shirtNumber": "13", "actionType": "substitution", "subType": "in", "success": 1}
        metadata = json.dumps({"player": "A. Lippasaar", "playerId": "live-216", "pno": "unverified-pno", "shirtNumber": "1", "tno": "1", "arcIdentityResolution": {"primary": "resolved", "secondary": "not_applicable"}})
        self.db.execute("INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result,metadata_json) VALUES('identity-game',19,1,'05:00',2,NULL,'substitution','success',?)", (metadata,))
        result = prepare_team_substitutions([(19, event)], "1", self.db, {}, "identity-game", "2", rosters)
        self.assertEqual(result[0]["player_id"], "216")

    def test_persisted_ambiguous_import_status_is_not_re_resolved(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 20, "period": 1, "clock": "05:00", "tno": "1", "player": "A. Lippasaar", "playerId": "live-216", "shirtNumber": "1", "actionType": "substitution", "subType": "in", "success": 1}
        metadata = json.dumps({"player": "A. Lippasaar", "arcIdentityResolution": {"primary": "ambiguous", "secondary": "not_applicable"}})
        self.db.execute("INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result,metadata_json) VALUES('identity-game',20,1,'05:00',2,NULL,'substitution','success',?)", (metadata,))
        with self.assertRaises(LineupTransitionError) as caught:
            prepare_team_substitutions([(20, event)], "1", self.db, {}, "identity-game", "2", rosters)
        self.assertEqual(caught.exception.code, FAILURE_AMBIGUOUS_PLAYER_IDENTITY)

    def test_persisted_event_team_and_stale_provider_mapping_are_rejected(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 18, "period": 1, "clock": "05:00", "tno": "1", "player": "A. Lippasaar", "playerId": "live-216", "shirtNumber": "1", "actionType": "substitution", "subType": "in", "success": 1}
        self.db.execute("INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES('identity-game',18,1,'05:00',3,216,'substitution','success')")
        with self.assertRaises(LineupTransitionError) as caught:
            prepare_team_substitutions([(18, event)], "1", self.db, {}, "identity-game", "2", rosters)
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)
        self.db.execute("UPDATE events SET team_id=2 WHERE game_id='identity-game' AND action_number=18")
        self.db.execute("UPDATE player_games SET provider_player_id='stale-id' WHERE game_id='identity-game' AND player_id=216")
        with self.assertRaises(LineupTransitionError) as caught:
            prepare_team_substitutions([(18, event)], "1", self.db, {}, "identity-game", "2", rosters)
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)

    def test_starter_resolution_uses_game_team_roster_and_validates_persisted_ids(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Starter opponent')")
        self.db.execute("INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('starter-game',1,1,2,3,'boxscore_imported')")
        self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(99,'Wrong persisted starter')")
        team_players = {}
        for pid in range(1, 6):
            name = f"Starter {pid} Player"
            self.db.execute("UPDATE players SET canonical_name=? WHERE player_id=?", (name, pid))
            team_players[str(pid)] = {"name": name, "firstName": f"Starter {pid}", "familyName": "Player", "shirtNumber": str(pid), "starter": 1}
            self.db.execute("INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('starter-game',?,2,?,'Team 2',?,1)", (pid, name, str(pid)))
        roster = {"tm": {"1": {"name": "Team 2", "pl": team_players}}}
        self.assertEqual(set(build_team_roster(roster, "1", self.db, {}, "starter-game", "2")), {"1", "2", "3", "4", "5"})
        self.db.execute("UPDATE player_games SET player_id=99 WHERE game_id='starter-game' AND shirt_number='1'")
        with self.assertRaises(LineupTransitionError) as caught:
            build_team_roster(roster, "1", self.db, {}, "starter-game", "2")
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)

    def test_starter_name_only_ambiguity_is_not_guessed(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        rosters["1"]["pl"]["starter-extra"] = {"name": "A. Lippasaar", "starter": 1}
        self.db.execute("UPDATE player_games SET starter=1 WHERE game_id='identity-game' AND player_id IN (125,216)")
        with self.assertRaises(LineupTransitionError) as caught:
            build_team_roster({"tm": rosters}, "1", self.db, {}, "identity-game", "2")
        self.assertEqual(caught.exception.code, FAILURE_AMBIGUOUS_PLAYER_IDENTITY)

    def test_starter_conflicting_jersey_is_rejected(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Starter opponent')")
        self.db.execute("INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('starter-conflict',1,1,2,3,'boxscore_imported')")
        roster = {}
        for pid in range(1, 6):
            name = f"Conflict Starter {pid}"
            self.db.execute("UPDATE players SET canonical_name=? WHERE player_id=?", (name, pid))
            self.db.execute("INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('starter-conflict',?,2,?,'Team 2',?,1)", (pid, name, str(pid)))
            roster[str(pid)] = {"name": name, "shirtNumber": "99" if pid == 1 else str(pid), "starter": 1}
        with self.assertRaises(LineupTransitionError) as caught:
            build_team_roster({"tm": {"1": {"name": "Team 2", "pl": roster}}}, "1", self.db, {}, "starter-conflict", "2")
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)

    def test_missing_and_duplicate_starting_occurrences_fail_explicitly(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Starter opponent')")
        self.db.execute("INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('starter-incomplete',1,1,2,3,'boxscore_imported')")
        roster = {}
        for pid in range(1, 5):
            name = f"Incomplete Starter {pid}"
            self.db.execute("UPDATE players SET canonical_name=? WHERE player_id=?", (name, pid))
            self.db.execute("INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('starter-incomplete',?,2,?,'Team 2',?,1)", (pid, name, str(pid)))
            roster[str(pid)] = {"name": name, "shirtNumber": str(pid), "starter": 1}
        with self.assertRaises(LineupTransitionError) as caught:
            build_team_roster({"tm": {"1": {"name": "Team 2", "pl": roster}}}, "1", self.db, {}, "starter-incomplete", "2")
        self.assertEqual(caught.exception.code, FAILURE_MISSING_STARTER)

    def test_conflicting_provider_and_jersey_identifiers_are_rejected(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 12, "tno": "1", "player": "A. Lippasaar", "playerId": "live-125", "shirtNumber": "1"}
        with self.assertRaises(LineupTransitionError) as caught:
            resolve_substitution_occurrence(self.db, "identity-game", "2", event, rosters)
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)

    def test_name_and_jersey_conflict_is_rejected(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 13, "tno": "1", "player": "Arvan-Johhannes Lippasaar", "shirtNumber": "1"}
        with self.assertRaises(LineupTransitionError) as caught:
            resolve_substitution_occurrence(self.db, "identity-game", "2", event, rosters)
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)

    def test_provider_identifier_from_another_team_is_rejected(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        rosters["2"] = {"pl": {"opponent": {"name": "Opponent Player", "playerId": "foreign-id", "shirtNumber": "9"}}}
        event = {"actionNumber": 14, "tno": "1", "player": "Other Name", "playerId": "foreign-id"}
        with self.assertRaises(LineupTransitionError) as caught:
            resolve_substitution_occurrence(self.db, "identity-game", "2", event, rosters)
        self.assertEqual(caught.exception.code, FAILURE_PLAYER_IDENTITY_CONFLICT)

    def test_unmatched_identity_is_explicitly_unresolved(self) -> None:
        rosters = self.add_lippasaar_occurrences()
        event = {"actionNumber": 15, "tno": "1", "player": "Unknown Player", "shirtNumber": "99"}
        with self.assertRaises(LineupTransitionError) as caught:
            resolve_substitution_occurrence(self.db, "identity-game", "2", event, rosters)
        self.assertEqual(caught.exception.code, FAILURE_UNRESOLVED_PLAYER_IDENTITY)

    def test_get_or_create_lineup_reuses_existing_db_lineup_when_cache_empty(self) -> None:
        players = ["1", "2", "3", "4", "5"]
        h = lineup_hash_for_players(players)
        self.db.execute(
            """
            INSERT INTO lineups (team_id, player_1, player_2, player_3, player_4, player_5, lineup_hash)
            VALUES (2, 1, 2, 3, 4, 5, ?)
            """,
            (h,),
        )
        self.db.commit()

        # Cache is empty (simulating cache miss or type mismatch)
        cache: dict[tuple[str, str], int] = {}
        lineup_id, created = get_or_create_lineup(self.db, cache, "2", players)

        self.assertFalse(created)
        self.assertEqual(lineup_id, 1)
        self.assertEqual(cache.get(("2", h)), 1)
        # Ensure no second row was inserted
        count = self.db.execute("SELECT COUNT(*) FROM lineups").fetchone()[0]
        self.assertEqual(count, 1)

    def test_get_or_create_lineup_string_and_int_team_id_compatibility(self) -> None:
        players = ["1", "2", "3", "4", "5"]
        cache: dict[tuple[str, str], int] = {}

        # First call with str team_id creates lineup
        lid1, created1 = get_or_create_lineup(self.db, cache, "2", players)
        self.assertTrue(created1)

        # Second call with int team_id reuses lineup
        lid2, created2 = get_or_create_lineup(self.db, cache, 2, players)
        self.assertFalse(created2)
        self.assertEqual(lid1, lid2)

    def test_overtime_numeric_periods_do_not_merge_with_regular_periods(self) -> None:
        events = [
            (1, {"actionNumber": 1, "period": 1, "periodType": "REGULAR", "actionType": "period"}),
            (2, {"actionNumber": 100, "period": 4, "periodType": "REGULAR", "actionType": "period"}),
            (3, {"actionNumber": 101, "period": 1, "periodType": "OVERTIME", "actionType": "period"}),
            (4, {"actionNumber": 150, "period": 1, "periodType": "OVERTIME", "actionType": "substitution"}),
            (5, {"actionNumber": 151, "period": 2, "periodType": "OVERTIME", "actionType": "period"}),
        ]

        grouped = group_period_events(extract_sorted_events({"pbp": [event for _, event in reversed(events)]}))

        self.assertEqual(list(grouped), [1, 4, 5, 6])
        self.assertEqual([event[1]["actionNumber"] for event in grouped[1]], [1])
        self.assertEqual([event[1]["actionNumber"] for event in grouped[5]], [101, 150])
        self.assertEqual(logical_period_number({"period": 1, "periodType": "OVERTIME"}), 5)
        self.assertEqual(logical_period_number({"period": 5, "periodType": "OVERTIME"}), 5)
        self.assertEqual(logical_period_number({"period": 1, "periodType": "REGULAR"}), 1)

    def test_extract_sorted_events_uses_source_order_when_action_numbers_disagree(self) -> None:
        chronological = [
            {"actionNumber": 2, "period": 1, "clock": "10:00:00", "actionType": "period"},
            {"actionNumber": 223, "period": 1, "clock": "02:27:00", "actionType": "substitution"},
            {"actionNumber": 224, "period": 1, "clock": "02:27:00", "actionType": "substitution"},
            {"actionNumber": 225, "period": 1, "clock": "00:49:20", "actionType": "substitution"},
            {"actionNumber": 226, "period": 1, "clock": "00:49:20", "actionType": "substitution"},
            {"actionNumber": 183, "period": 1, "clock": "00:42:60", "actionType": "substitution"},
            {"actionNumber": 227, "period": 1, "clock": "00:42:60", "actionType": "substitution"},
            {"actionNumber": 187, "period": 1, "clock": "00:21:30", "actionType": "2pt"},
            {"actionNumber": 192, "period": 1, "clock": "00:00:00", "actionType": "period"},
        ]

        events = extract_sorted_events({"pbp": list(reversed(chronological))})

        self.assertEqual([event["actionNumber"] for _, event in events], [event["actionNumber"] for event in chronological])

    def test_extract_sorted_events_rejects_duplicate_action_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate LiveStats actionNumber 183"):
            extract_sorted_events({"pbp": [{"actionNumber": 183}, {"actionNumber": 183}]})

    def test_deferred_incoming_lookup_uses_source_position_not_action_number(self) -> None:
        events = [
            (10, {"actionNumber": 183, "actionType": "substitution", "subType": "out", "tno": "1"}),
            (9, {"actionNumber": 182, "actionType": "substitution", "subType": "in", "tno": "1"}),
        ]

        self.assertEqual(find_next_incoming_before_resume(events, "1", 10), 182)

    def test_substitutions_without_valid_clocks_fail_closed(self) -> None:
        for clock in (None, "not-a-clock"):
            event = {
                "actionNumber": 1,
                "actionType": "substitution",
                "subType": "in",
                "player": "Player 6",
                "tno": "1",
                "clock": clock,
            }
            with self.subTest(clock=clock), self.assertRaisesRegex(ValueError, "Missing or invalid substitution clock"):
                prepare_team_substitutions([(1, event)], "1", self.db, {})

    def test_substitutions_without_valid_team_or_success_fail_closed(self) -> None:
        base_event = {
            "actionNumber": 1,
            "actionType": "substitution",
            "subType": "in",
            "player": "Player 6",
            "clock": "05:00:00",
        }
        for event, expected in (
            ({**base_event, "tno": None}, "Missing or invalid substitution team"),
            ({**base_event, "tno": "1", "success": 0}, "Unsuccessful substitution event"),
        ):
            with self.subTest(event=event), self.assertRaisesRegex(ValueError, expected):
                prepare_team_substitutions([(1, event)], "1", self.db, {})

    def test_2155878_actual_source_order_controls_batches_and_segment_boundaries(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Away team')")
        home_names = {
            93: "Artis Ate",
            94: "Alberts Putans",
            95: "Emils Krumins",
            97: "Nysier Brooks",
            121: "Maris Gulbis",
            149: "Kristers Zoriks",
            239: "Elijah Clarance",
        }
        away_names = {player_id: f"Away player {player_id}" for player_id in range(1001, 1006)}
        game_id = "estlatbl_2023212162"
        self.db.execute(
            "INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES(?,1,1,2,3,'boxscore_imported')",
            (game_id,),
        )
        self.db.execute(
            "INSERT INTO source_livestats_games(game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES(?,1,1,'2023212162','2155878','mock://2155878')",
            (game_id,),
        )
        home_starters = {93, 95, 97, 121, 239}
        for player_id, name in {**home_names, **away_names}.items():
            self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(?,?)", (player_id, name))
            team_id = 2 if player_id in home_names else 3
            starter = int(player_id in (home_starters if team_id == 2 else set(away_names)))
            self.db.execute(
                "INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES(?,?,?,?,?,?,?)",
            (game_id, player_id, team_id, name, "Home" if team_id == 2 else "Away", {93: "15", 94: "25", 121: "13", 149: "4"}.get(player_id, str(player_id)), starter),
            )
        substitutions = [
            {"actionNumber": 223, "period": 1, "periodType": "REGULAR", "clock": "02:27:00", "gt": "02:27", "previousAction": "", "tno": 1, "pno": 6, "actionType": "substitution", "subType": "out", "player": "A. Ate", "shirtNumber": "15", "success": 1, "qualifier": []},
            {"actionNumber": 224, "period": 1, "periodType": "REGULAR", "clock": "02:27:00", "gt": "02:27", "previousAction": "", "tno": 1, "pno": 7, "actionType": "substitution", "subType": "in", "player": "A. Putans", "shirtNumber": "25", "success": 1, "qualifier": []},
            {"actionNumber": 225, "period": 1, "periodType": "REGULAR", "clock": "00:49:20", "gt": "00:49", "previousAction": "", "tno": 1, "pno": 7, "actionType": "substitution", "subType": "out", "player": "A. Putans", "shirtNumber": "25", "success": 1, "qualifier": []},
            {"actionNumber": 226, "period": 1, "periodType": "REGULAR", "clock": "00:49:20", "gt": "00:49", "previousAction": "", "tno": 1, "pno": 6, "actionType": "substitution", "subType": "in", "player": "A. Ate", "shirtNumber": "15", "success": 1, "qualifier": []},
            {"actionNumber": 183, "period": 1, "periodType": "REGULAR", "clock": "00:42:60", "gt": "00:42", "previousAction": "", "tno": 1, "pno": 12, "actionType": "substitution", "subType": "in", "player": "K. Zoriks", "shirtNumber": "4", "success": 1, "qualifier": []},
            {"actionNumber": 227, "period": 1, "periodType": "REGULAR", "clock": "00:42:60", "gt": "00:42", "previousAction": "", "tno": 1, "pno": 6, "actionType": "substitution", "subType": "out", "player": "A. Ate", "shirtNumber": "15", "success": 1, "qualifier": []},
        ]
        chronological = [
            {"actionNumber": 2, "period": 1, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start", "success": 1},
            *substitutions,
            {"actionNumber": 187, "period": 1, "periodType": "REGULAR", "clock": "00:21:30", "gt": "00:21", "previousAction": "", "tno": 1, "pno": 4, "actionType": "2pt", "subType": "fadeaway", "player": "M. Gulbis", "shirtNumber": "13", "success": 1, "qualifier": ["fromturnover", "pointsinthepaint"]},
            {"actionNumber": 192, "period": 1, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end", "success": 1},
        ]
        game_data = {
            "tm": {
                "1": {"name": "Home", "pl": {str(pid): {"name": name, "shirtNumber": {93: "15", 94: "25", 121: "13", 149: "4"}.get(pid, str(pid)), "starter": int(pid in home_starters)} for pid, name in home_names.items()}},
                "2": {"name": "Away", "pl": {str(pid): {"name": name, "starter": 1} for pid, name in away_names.items()}},
            },
            "pbp": list(reversed(chronological)),
        }
        for event in substitutions:
            player_id = next(pid for pid, name in home_names.items() if name.endswith(event["player"].split()[-1]))
            self.db.execute(
                "INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES(?,?,?,?,2,?,'substitution','success')",
                (game_id, event["actionNumber"], event["period"], event["clock"], player_id),
            )
        self.db.commit()
        self.db.row_factory = sqlite3.Row
        game_row = self.db.execute(
            "SELECT g.game_id,g.home_team_id,g.away_team_id,s.json_url FROM games g JOIN source_livestats_games s USING(game_id) WHERE g.game_id=?",
            (game_id,),
        ).fetchone()
        reconstructor = __import__("lineup_reconstructor")
        original_fetch = reconstructor.fetch_json
        reconstructor.fetch_json = lambda _: game_data
        try:
            process_game(self.db, game_row, build_player_lookup(self.db), {}, {})
        finally:
            reconstructor.fetch_json = original_fetch

        segments = self.db.execute(
            """
            SELECT s.start_action_number,s.end_action_number,s.start_clock,s.end_clock,
                   l.player_1,l.player_2,l.player_3,l.player_4,l.player_5
            FROM lineup_segments s JOIN lineups l USING(lineup_id)
            WHERE s.game_id=? AND s.team_id=2 AND s.period=1
            ORDER BY s.start_clock DESC
            """,
            (game_id,),
        ).fetchall()
        self.assertEqual(len(segments), 4)
        self.assertEqual([(r[0], r[1], r[2], r[3]) for r in segments], [
            (2, 223, "10:00:00", "02:27:00"),
            (224, 225, "02:27:00", "00:49:20"),
            (226, 183, "00:49:20", "00:42:60"),
            (227, 192, "00:42:60", "00:00:00"),
        ])
        self.assertEqual(set(map(str, segments[-1][4:])), {"95", "97", "121", "149", "239"})
        for row in self.db.execute("SELECT player_1,player_2,player_3,player_4,player_5 FROM lineups"):
            self.assertEqual(len(set(map(str, row))), 5)

    def test_prior_source_batches_and_successful_2155684_burst_keep_final_five(self) -> None:
        cases = (
            (
                "2155683",
                {"139", "140", "141", "144", "154"},
                [
                    {"actionNumber": 530, "index": 1, "period": 3, "clock": "00:11:40", "tno": "1", "previousAction": "", "subType": "in", "player_id": "137", "player": "K. Lips", "shirtNumber": "9", "success": 1},
                    {"actionNumber": 531, "index": 2, "period": 3, "clock": "00:11:40", "tno": "1", "previousAction": "", "subType": "out", "player_id": "141", "player": "D. Harris", "shirtNumber": "3", "success": 1},
                ],
                {"137", "139", "140", "144", "154"},
            ),
            (
                "2155684",
                {"62", "63", "64", "134", "193"},
                [
                    {"actionNumber": 484, "index": 1, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "out", "player_id": "62", "player": "J. Heinla", "shirtNumber": "32", "success": 1},
                    {"actionNumber": 485, "index": 2, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "out", "player_id": "64", "player": "K. Reinart", "shirtNumber": "33", "success": 1},
                    {"actionNumber": 486, "index": 3, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "out", "player_id": "63", "player": "R. K\u00fcttis", "shirtNumber": "35", "success": 1},
                    {"actionNumber": 487, "index": 4, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "in", "player_id": "58", "player": "E. Keedus", "shirtNumber": "0", "success": 1},
                    {"actionNumber": 488, "index": 5, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "in", "player_id": "65", "player": "K. Gluditis", "shirtNumber": "11", "success": 1},
                    {"actionNumber": 489, "index": 6, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "in", "player_id": "60", "player": "G. Ilves", "shirtNumber": "14", "success": 1},
                    {"actionNumber": 492, "index": 7, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "out", "player_id": "134", "player": "M. Hymon", "shirtNumber": "2", "success": 1},
                    {"actionNumber": 493, "index": 8, "period": 3, "clock": "00:07:90", "tno": "1", "previousAction": "", "subType": "in", "player_id": "61", "player": "T. Raadik", "shirtNumber": "12", "success": 1},
                ],
                {"58", "60", "61", "65", "193"},
            ),
            (
                "2155692",
                {"67", "70", "72", "73", "246"},
                [
                    {"actionNumber": 103, "index": 1, "period": 1, "clock": "02:56:00", "tno": "2", "previousAction": "", "subType": "in", "player_id": "197", "player": "R. Vitolskis", "shirtNumber": "1", "success": 1},
                    {"actionNumber": 101, "index": 2, "period": 1, "clock": "02:56:00", "tno": "2", "previousAction": "", "subType": "out", "player_id": "73", "player": "R. Avotins", "shirtNumber": "5", "success": 1},
                    {"actionNumber": 104, "index": 3, "period": 1, "clock": "02:56:00", "tno": "2", "previousAction": "", "subType": "in", "player_id": "186", "player": "K. Cavars", "shirtNumber": "8", "success": 1},
                    {"actionNumber": 112, "index": 4, "period": 1, "clock": "02:56:00", "tno": "2", "previousAction": "", "subType": "out", "player_id": "70", "player": "T. Skuja", "shirtNumber": "3", "success": 1},
                ],
                {"67", "72", "186", "197", "246"},
            ),
            (
                "2155694",
                {"172", "175", "176", "179", "221"},
                [
                    {"actionNumber": 703, "index": 1, "period": 4, "clock": "01:58:00", "tno": "1", "previousAction": "", "subType": "in", "player_id": "173", "player": "K. Kitsing", "shirtNumber": "13", "success": 1},
                    {"actionNumber": 706, "index": 2, "period": 4, "clock": "01:58:00", "tno": "1", "previousAction": "", "subType": "out", "player_id": "176", "player": "A. Konontsuk", "shirtNumber": "9", "success": 1},
                ],
                {"172", "173", "175", "179", "221"},
            ),
        )

        for match_id, before, batch, expected in cases:
            with self.subTest(match_id=match_id):
                lineup = set(before)
                after = apply_substitution_batch(lineup, batch, "home", 1, "00:07:90")
                self.assertEqual(after, expected)
                self.assertEqual(len(after), 5)
                self.assertEqual(lineup, before)

    def test_substitution_batch_is_atomic_and_uses_player_ids(self) -> None:
        lineup = {"1", "2", "3", "4", "5"}
        batch = [
            {"actionNumber": 10, "index": 1, "subType": "out", "player_id": "2"},
            {"actionNumber": 11, "index": 2, "subType": "out", "player_id": "3"},
            {"actionNumber": 12, "index": 3, "subType": "in", "player_id": "6"},
            {"actionNumber": 13, "index": 4, "subType": "in", "player_id": "7"},
        ]

        result = apply_substitution_batch(lineup, batch, "away", 2, "06:11:00", game_id="game-a")

        self.assertEqual(lineup, {"1", "2", "3", "4", "5"})
        self.assertEqual(result, {"1", "4", "5", "6", "7"})
        self.assertEqual(len(result), 5)

    def test_multiple_sequential_substitution_pairs_keep_a_five_player_final_lineup(self) -> None:
        lineup = {"1", "2", "3", "4", "5"}
        batch = [
            {"actionNumber": 10, "index": 1, "subType": "out", "player_id": "2"},
            {"actionNumber": 11, "index": 2, "subType": "in", "player_id": "6"},
            {"actionNumber": 12, "index": 3, "subType": "out", "player_id": "3"},
            {"actionNumber": 13, "index": 4, "subType": "in", "player_id": "7"},
        ]

        result = apply_substitution_batch(lineup, batch, "home", 2, "06:11:00")

        self.assertEqual(result, {"1", "4", "5", "6", "7"})
        self.assertEqual(lineup, {"1", "2", "3", "4", "5"})

    def test_in_then_out_for_a_new_player_is_valid_within_one_atomic_stoppage(self) -> None:
        lineup = {"1", "2", "3", "4", "5"}
        batch = [
            {"actionNumber": 10, "index": 1, "subType": "in", "player_id": "6"},
            {"actionNumber": 11, "index": 2, "subType": "out", "player_id": "6"},
        ]

        result = apply_substitution_batch(lineup, batch, "home", 1, "05:00:00")

        self.assertEqual(result, lineup)
        self.assertEqual(lineup, {"1", "2", "3", "4", "5"})

    def test_2155673_ordered_batch_allows_a_player_to_leave_and_reenter(self) -> None:
        lineup = {"54", "55", "56", "200", "201"}
        batch = [
            {"actionNumber": 578, "index": 1, "subType": "out", "player_id": "200"},
            {"actionNumber": 579, "index": 2, "subType": "in", "player_id": "53"},
            {"actionNumber": 580, "index": 3, "subType": "out", "player_id": "54"},
            {"actionNumber": 581, "index": 4, "subType": "in", "player_id": "200"},
        ]

        result = apply_substitution_batch(
            lineup,
            batch,
            "home",
            3,
            "00:24:30",
            game_id="estlatbl_2023212132",
        )

        self.assertEqual(result, {"53", "55", "56", "200", "201"})
        self.assertEqual(lineup, {"54", "55", "56", "200", "201"})

    def test_2155673_actual_events_create_one_final_lineup_segment_boundary(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Away team')")
        home_names = {
            53: "Valters Veveris",
            54: "Kristers Elksnitis",
            55: "Home player 55",
            56: "Home player 56",
            200: "Kristofers Karlsons",
            201: "Home player 201",
        }
        away_names = {player_id: f"Away player {player_id}" for player_id in range(11, 16)}
        self.db.execute(
            "INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('estlatbl_2023212132',1,1,2,3,'boxscore_imported')"
        )
        self.db.execute(
            "INSERT INTO source_livestats_games(game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES('estlatbl_2023212132',1,1,'2023212132','2155673','mock://2155673')"
        )
        for player_id, name in {**home_names, **away_names}.items():
            self.db.execute(
                "INSERT INTO players(player_id,canonical_name) VALUES(?,?)",
                (player_id, name),
            )
            team_id = 2 if player_id in home_names else 3
            starter = int(player_id in ({54, 55, 56, 200, 201} if team_id == 2 else set(away_names)))
            self.db.execute(
                "INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('estlatbl_2023212132',?,?,?,?,?,?)",
                (player_id, team_id, name, "Home" if team_id == 2 else "Away", str(player_id), starter),
            )
        for player_id, alias in {
            53: "V. Veveris",
            54: "K. Elksnitis",
            200: "K. Karlsons",
        }.items():
            self.db.execute(
                "INSERT INTO player_aliases(player_id,alias_name,source) VALUES(?,?,'fiba_livestats')",
                (player_id, alias),
            )
        self.db.row_factory = sqlite3.Row
        events = [
            {"actionNumber": 368, "period": 3, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
            {"actionNumber": 578, "period": 3, "periodType": "REGULAR", "clock": "00:24:30", "tno": "1", "actionType": "substitution", "subType": "out", "player": "K. Karlsons"},
            {"actionNumber": 579, "period": 3, "periodType": "REGULAR", "clock": "00:24:30", "tno": "1", "actionType": "substitution", "subType": "in", "player": "V. Veveris"},
            {"actionNumber": 580, "period": 3, "periodType": "REGULAR", "clock": "00:24:30", "tno": "1", "actionType": "substitution", "subType": "out", "player": "K. Elksnitis"},
            {"actionNumber": 581, "period": 3, "periodType": "REGULAR", "clock": "00:24:30", "tno": "1", "actionType": "substitution", "subType": "in", "player": "K. Karlsons"},
            {"actionNumber": 588, "period": 3, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
        ]
        game_data = {
            "tm": {
                "1": {"name": "Home", "pl": {str(pid): {"name": name, "starter": int(pid in {54, 55, 56, 200, 201})} for pid, name in home_names.items()}},
                "2": {"name": "Away", "pl": {str(pid): {"name": name, "starter": 1} for pid, name in away_names.items()}},
            },
            "pbp": events,
        }
        game_row = self.db.execute(
            "SELECT g.game_id,g.home_team_id,g.away_team_id,s.json_url FROM games g JOIN source_livestats_games s USING(game_id) WHERE g.game_id='estlatbl_2023212132'"
        ).fetchone()
        reconstructor = __import__("lineup_reconstructor")
        original_fetch = reconstructor.fetch_json
        reconstructor.fetch_json = lambda _: {**game_data, "pbp": list(reversed(game_data["pbp"]))}
        try:
            process_game(self.db, game_row, build_player_lookup(self.db), {}, {})
        finally:
            reconstructor.fetch_json = original_fetch

        segments = self.db.execute(
            """
            SELECT s.start_action_number,s.end_action_number,s.start_clock,s.end_clock,
                   l.player_1,l.player_2,l.player_3,l.player_4,l.player_5
            FROM lineup_segments s JOIN lineups l USING(lineup_id)
            WHERE s.game_id='estlatbl_2023212132' AND s.team_id=2 AND s.period=3
            ORDER BY s.start_action_number
            """
        ).fetchall()
        self.assertEqual(len(segments), 2)
        self.assertEqual((segments[0][0], segments[0][1], segments[0][2], segments[0][3]), (368, 578, "10:00:00", "00:24:30"))
        self.assertEqual((segments[1][0], segments[1][1], segments[1][2], segments[1][3]), (581, 588, "00:24:30", "00:00:00"))
        self.assertEqual(set(map(str, segments[1][4:])), {"53", "55", "56", "200", "201"})
        for row in self.db.execute("SELECT player_1,player_2,player_3,player_4,player_5 FROM lineups"):
            self.assertEqual(len(set(map(str, row))), 5)

    def test_reentry_out_for_a_player_who_is_already_off_court_is_rejected(self) -> None:
        lineup = {"54", "55", "56", "200", "201"}
        invalid_batch = [
            {"actionNumber": 578, "index": 1, "subType": "out", "player_id": "200"},
            {"actionNumber": 579, "index": 2, "subType": "in", "player_id": "53"},
            {"actionNumber": 580, "index": 3, "subType": "out", "player_id": "200"},
            {"actionNumber": 581, "index": 4, "subType": "in", "player_id": "200"},
        ]

        with self.assertRaisesRegex(ValueError, "not active at action 580"):
            apply_substitution_batch(lineup, invalid_batch, "home", 3, "00:24:30")

        self.assertEqual(lineup, {"54", "55", "56", "200", "201"})

    def test_normal_substitution_and_same_clock_actions_are_one_ordered_batch(self) -> None:
        substitutions = [
            {"actionNumber": 12, "index": 4, "period": 2, "clock": "06:11:00", "subType": "in", "player_id": "7"},
            {"actionNumber": 10, "index": 2, "period": 2, "clock": "06:11:00", "subType": "out", "player_id": "2"},
            {"actionNumber": 11, "index": 3, "period": 2, "clock": "06:11:00", "subType": "out", "player_id": "3"},
            {"actionNumber": 13, "index": 5, "period": 2, "clock": "06:11:00", "subType": "in", "player_id": "6"},
        ]
        groups = group_substitutions_by_batch(substitutions)
        self.assertEqual(len(groups), 1)
        self.assertEqual([item["actionNumber"] for item in groups[0]], [12, 10, 11, 13])

        lineup = {"1", "2", "3", "4", "5"}
        result = apply_substitution_batch(lineup, groups[0], "away", 2, "06:11:00")
        self.assertEqual(result, {"1", "4", "5", "6", "7"})
        self.assertEqual(lineup, {"1", "2", "3", "4", "5"})

    def test_four_and_six_player_batches_fail_without_mutating_previous_state(self) -> None:
        cases = (
            ([{"actionNumber": 1, "index": 1, "subType": "out", "player_id": "1"}], "4 players"),
            ([{"actionNumber": 1, "index": 1, "subType": "in", "player_id": "6"}], "6 players"),
        )
        for batch, expected in cases:
            lineup = {"1", "2", "3", "4", "5"}
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                apply_substitution_batch(lineup, batch, "away", 1, "05:00:00")
            self.assertEqual(lineup, {"1", "2", "3", "4", "5"})

    def test_outgoing_player_must_be_active_at_each_ordered_event(self) -> None:
        lineup = {"1", "2", "3", "4", "5"}
        batch = [
            {"actionNumber": 1, "index": 1, "subType": "out", "player_id": "2"},
            {"actionNumber": 2, "index": 2, "subType": "out", "player_id": "2"},
            {"actionNumber": 3, "index": 3, "subType": "in", "player_id": "6"},
            {"actionNumber": 4, "index": 4, "subType": "in", "player_id": "7"},
        ]
        with self.assertRaisesRegex(ValueError, "not active at action 2"):
            apply_substitution_batch(lineup, batch, "away", 1, "05:00:00")
        self.assertEqual(lineup, {"1", "2", "3", "4", "5"})

    def test_process_game_savepoint_rolls_back_segments_and_caches_on_bad_batch(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Away team')")
        self.db.execute(
            "INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('rollback-game',1,1,2,3,'pending')"
        )
        self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(99,'Not active')")
        for player_id in range(1, 6):
            self.db.execute("UPDATE players SET canonical_name=? WHERE player_id=?", (f"Home {player_id}", player_id))
            self.db.execute(
                "INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('rollback-game',?,2,?,?,?,1)",
                (player_id, f"Home {player_id}", "Home team", str(player_id)),
            )
        for player_id in range(11, 16):
            self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(?,?)", (player_id, f"Away {player_id}"))
            self.db.execute(
                "INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('rollback-game',?,3,?,?,?,1)",
                (player_id, f"Away {player_id}", "Away team", str(player_id)),
            )
        self.db.execute("INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('rollback-game',99,3,'Not active','Away team','99',0)")
        self.db.execute(
            "INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES('rollback-game',2,1,'05:00:00',3,99,'substitution','success')"
        )
        self.db.commit()
        self.db.row_factory = sqlite3.Row

        game_row = {
            "game_id": "rollback-game",
            "home_team_id": "2",
            "away_team_id": "3",
            "json_url": "mock://rollback",
        }
        live_data = {
            "tm": {
                "1": {"name": "Home team", "pl": {str(pid): {"name": f"Home {pid}", "starter": 1} for pid in range(1, 6)}},
                "2": {"name": "Away team", "pl": {**{str(pid): {"name": f"Away {pid}", "shirtNumber": str(pid), "starter": 1} for pid in range(11, 16)}, "99": {"name": "Not active", "shirtNumber": "99", "starter": 0}}},
            },
            "pbp": [
                {"actionNumber": 1, "period": 1, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period"},
                {"actionNumber": 2, "period": 1, "periodType": "REGULAR", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Not active", "shirtNumber": "99"},
                {"actionNumber": 3, "period": 1, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period"},
            ],
        }
        lineup_cache = {("existing-team", "existing-hash"): 77}
        segment_cache = {"existing-segment": 88}
        module = __import__("lineup_reconstructor")
        original_fetch = module.fetch_json
        module.fetch_json = lambda _: {**live_data, "pbp": list(reversed(live_data["pbp"]))}
        try:
            with self.assertRaisesRegex(LineupTransitionError, "outgoing players are not active"):
                process_game(self.db, game_row, {f"Home {pid}": str(pid) for pid in range(1, 6)} | {f"Away {pid}": str(pid) for pid in range(11, 16)}, lineup_cache, segment_cache)
        finally:
            module.fetch_json = original_fetch

        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM lineups").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM lineup_segments WHERE game_id='rollback-game'").fetchone()[0], 0)
        self.assertEqual(lineup_cache, {("existing-team", "existing-hash"): 77})
        self.assertEqual(segment_cache, {"existing-segment": 88})

    def test_substitutions_with_clock_format_variants_stay_in_one_batch(self) -> None:
        substitutions = [
            {"actionNumber": 2, "index": 2, "period": 2, "clock": "06:11", "subType": "in", "player_id": "6"},
            {"actionNumber": 1, "index": 1, "period": 2, "clock": "06:11:00", "subType": "out", "player_id": "1"},
        ]
        groups = group_substitutions_by_batch(substitutions)
        self.assertEqual(len(groups), 1)
        lineup = {"1", "2", "3", "4", "5"}
        self.assertEqual(
            apply_substitution_batch(lineup, groups[0], "home", 2, "06:11:00"),
            {"2", "3", "4", "5", "6"},
        )

    def test_clock_batch_key_keeps_distinct_subsecond_times_separate(self) -> None:
        self.assertEqual(clock_group_key("06:11"), clock_group_key("06:11:00"))
        self.assertNotEqual(clock_group_key("00:45:20"), clock_group_key("00:45:80"))

    def test_2155633_overtime_does_not_pollute_period_two_and_state_is_per_game(self) -> None:
        self.db.execute("UPDATE teams SET canonical_name='Home team' WHERE team_id=2")
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Away team')")
        names = {
            1: "Hendrik Eelmae", 2: "Robin Kivi", 3: "Mart Rosenthal",
            4: "Oliver Suurorg", 8: "Andrii Voinalovych", 9: "Jalan McCloud",
            10: "Tomas Pavelka", 204: "Leemet Bockler", 213: "Brandon Johnson",
            101: "Home one", 102: "Home two", 103: "Home three",
            104: "Home four", 105: "Home five",
        }
        for player_id, name in names.items():
            if self.db.execute("SELECT 1 FROM players WHERE player_id=?", (player_id,)).fetchone():
                self.db.execute("UPDATE players SET canonical_name=? WHERE player_id=?", (name, player_id))
            else:
                self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(?,?)", (player_id, name))

        home_players = [101, 102, 103, 104, 105]
        away_players = [1, 2, 3, 4, 8, 9, 10, 204, 213]
        away_starters = {4, 8, 9, 10, 213}
        data = {
            "tm": {
                "1": {"name": "Home team", "pl": {str(pid): {"name": names[pid], "starter": 1, "shirtNumber": str(pid)} for pid in home_players}},
                "2": {"name": "Away team", "pl": {str(pid): {"name": names[pid], "starter": int(pid in away_starters), "shirtNumber": str(pid)} for pid in away_players}},
            },
            "pbp": [
                {"actionNumber": 1, "period": 1, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 168, "period": 1, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
                {"actionNumber": 171, "period": 2, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 192, "period": 2, "periodType": "REGULAR", "clock": "08:36:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Andrii Voinalovych"},
                {"actionNumber": 193, "period": 2, "periodType": "REGULAR", "clock": "08:36:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Robin Kivi"},
                {"actionNumber": 226, "period": 2, "periodType": "REGULAR", "clock": "06:34:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Jalan McCloud"},
                {"actionNumber": 227, "period": 2, "periodType": "REGULAR", "clock": "06:34:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Mart Rosenthal"},
                {"actionNumber": 233, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Robin Kivi"},
                {"actionNumber": 234, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Tomas Pavelka"},
                {"actionNumber": 235, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Leemet Bockler"},
                {"actionNumber": 236, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Andrii Voinalovych"},
                {"actionNumber": 293, "period": 2, "periodType": "REGULAR", "clock": "02:56:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Brandon Johnson"},
                {"actionNumber": 294, "period": 2, "periodType": "REGULAR", "clock": "02:56:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Tomas Pavelka"},
                {"actionNumber": 333, "period": 2, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
                {"actionNumber": 342, "period": 3, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 484, "period": 3, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
                {"actionNumber": 487, "period": 4, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 657, "period": 4, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
                {"actionNumber": 660, "period": 1, "periodType": "OVERTIME", "clock": "05:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 694, "period": 1, "periodType": "OVERTIME", "clock": "02:41:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Tomas Pavelka"},
                {"actionNumber": 695, "period": 1, "periodType": "OVERTIME", "clock": "02:41:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Hendrik Eelmae"},
                {"actionNumber": 737, "period": 1, "periodType": "OVERTIME", "clock": "00:00:00", "actionType": "period", "subType": "end"},
            ],
        }

        for game_id in ("game-2155633", "game-second"):
            self.db.execute(
                "INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES(?,1,1,2,3,'boxscore_imported')",
                (game_id,),
            )
            self.db.execute(
                "INSERT INTO source_livestats_games(game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES(?,1,1,?,?,?)",
                (game_id, game_id, game_id, "mock://lineup"),
            )
            for player_id in home_players + away_players:
                team_id = 2 if player_id in home_players else 3
                self.db.execute(
                    "INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES(?,?,?,?,?,?,?)",
                    (game_id, player_id, team_id, names[player_id], "Home team" if team_id == 2 else "Away team", str(player_id), int(player_id in (set(home_players) if team_id == 2 else away_starters))),
                )
            for event in data["pbp"]:
                if event.get("actionType") != "substitution":
                    continue
                player_id = next(pid for pid, name in names.items() if name == event["player"])
                self.db.execute(
                    "INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES(?,?,?,?,?,?,'substitution','success')",
                    (game_id, event["actionNumber"], event["period"], event["clock"], 3, player_id),
                )
        self.db.commit()
        self.db.row_factory = sqlite3.Row
        original_fetch = __import__("lineup_reconstructor").fetch_json
        reconstructor = __import__("lineup_reconstructor")
        reconstructor.fetch_json = lambda _: {**data, "pbp": list(reversed(data["pbp"]))}
        try:
            for game_id in ("game-2155633", "game-second"):
                game_row = self.db.execute(
                    "SELECT g.game_id,g.home_team_id,g.away_team_id,s.json_url FROM games g JOIN source_livestats_games s USING(game_id) WHERE g.game_id=?",
                    (game_id,),
                ).fetchone()
                process_game(self.db, game_row, {name: str(pid) for pid, name in names.items()}, {}, {})
        finally:
            reconstructor.fetch_json = original_fetch

        for game_id in ("game-2155633", "game-second"):
            p2_segment = self.db.execute(
                "SELECT l.player_1,l.player_2,l.player_3,l.player_4,l.player_5 FROM lineup_segments s JOIN lineups l USING(lineup_id) WHERE s.game_id=? AND s.team_id=3 AND s.period=2 AND s.start_clock='06:11:00' AND s.end_clock='02:56:00'",
                (game_id,),
            ).fetchone()
            self.assertEqual(set(map(str, p2_segment)), {"3", "4", "8", "204", "213"})
            ot_segment = self.db.execute(
                "SELECT l.player_1,l.player_2,l.player_3,l.player_4,l.player_5 FROM lineup_segments s JOIN lineups l USING(lineup_id) WHERE s.game_id=? AND s.team_id=3 AND s.period=5 AND s.start_clock='02:41:00' AND s.end_clock='00:00:00'",
                (game_id,),
            ).fetchone()
            self.assertEqual(set(map(str, ot_segment)), {"1", "3", "4", "8", "204"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM lineup_segments WHERE game_id='game-2155633'").fetchone()[0], self.db.execute("SELECT COUNT(*) FROM lineup_segments WHERE game_id='game-second'").fetchone()[0])

    def test_lineup_state_uses_distinct_player_ids_not_display_names(self) -> None:
        lineup = {"player-kaspar", "player-kristjan", "3", "4", "5"}
        batch = [
            {"actionNumber": 1, "index": 1, "subType": "out", "player_id": "player-kaspar"},
            {"actionNumber": 2, "index": 2, "subType": "in", "player_id": "player-new"},
        ]
        result = apply_substitution_batch(lineup, batch, "home", 1, "05:00:00")
        self.assertEqual(result, {"player-kristjan", "player-new", "3", "4", "5"})
        self.assertIn("player-kristjan", result)
        self.assertNotIn("player-kaspar", result)

    def test_2155633_overtime_events_do_not_corrupt_period_two_substitution_state(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Away team')")
        names = {
            1: "Hendrik Eelmae", 2: "Robin Kivi", 3: "Mart Rosenthal",
            4: "Oliver Suurorg", 5: "Unused player", 8: "Andrii Voinalovych",
            9: "Jalan McCloud", 10: "Tomas Pavelka", 11: "Leemet Bockler",
            13: "Brandon Johnson", 101: "Home one", 102: "Home two",
            103: "Home three", 104: "Home four", 105: "Home five",
        }
        for player_id, name in names.items():
            if self.db.execute("SELECT 1 FROM players WHERE player_id=?", (player_id,)).fetchone():
                self.db.execute("UPDATE players SET canonical_name=? WHERE player_id=?", (name, player_id))
            else:
                self.db.execute(
                    "INSERT INTO players(player_id,canonical_name) VALUES(?,?)",
                    (player_id, name),
                )
        self.db.execute(
            "INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('game-2155633',1,1,2,3,'boxscore_imported')"
        )
        self.db.execute(
            "INSERT INTO source_livestats_games(game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES('game-2155633',1,1,'provider-2155633','2155633','mock://2155633')"
        )

        home_players = [101, 102, 103, 104, 105]
        away_players = [4, 8, 9, 10, 13, 2, 3, 11, 1]
        starters = {4, 8, 9, 10, 13, *home_players}
        for player_id in home_players + away_players:
            team_id = 2 if player_id in home_players else 3
            self.db.execute(
                "INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('game-2155633',?,?,?,?,?,?)",
                (player_id, team_id, names[player_id], "Home" if team_id == 2 else "Away", str(player_id), int(player_id in starters)),
            )

        data = {
            "tm": {
                "1": {"name": "Home", "pl": {str(pid): {"name": names[pid], "starter": 1} for pid in home_players}},
                "2": {"name": "Away", "pl": {str(pid): {"name": names[pid], "starter": int(pid in starters)} for pid in away_players}},
            },
            "pbp": [
                {"actionNumber": 1, "period": 1, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 150, "period": 1, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
                {"actionNumber": 171, "period": 2, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 192, "period": 2, "periodType": "REGULAR", "clock": "08:36:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Andrii Voinalovych"},
                {"actionNumber": 193, "period": 2, "periodType": "REGULAR", "clock": "08:36:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Robin Kivi"},
                {"actionNumber": 226, "period": 2, "periodType": "REGULAR", "clock": "06:34:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Jalan McCloud"},
                {"actionNumber": 227, "period": 2, "periodType": "REGULAR", "clock": "06:34:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Mart Rosenthal"},
                {"actionNumber": 233, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Robin Kivi"},
                {"actionNumber": 234, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Tomas Pavelka"},
                {"actionNumber": 235, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Leemet Bockler"},
                {"actionNumber": 236, "period": 2, "periodType": "REGULAR", "clock": "06:11:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Andrii Voinalovych"},
                {"actionNumber": 293, "period": 2, "periodType": "REGULAR", "clock": "02:56:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Brandon Johnson"},
                {"actionNumber": 294, "period": 2, "periodType": "REGULAR", "clock": "02:56:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Tomas Pavelka"},
                {"actionNumber": 333, "period": 2, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
                {"actionNumber": 487, "period": 4, "periodType": "REGULAR", "clock": "10:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 657, "period": 4, "periodType": "REGULAR", "clock": "00:00:00", "actionType": "period", "subType": "end"},
                {"actionNumber": 660, "period": 1, "periodType": "OVERTIME", "clock": "05:00:00", "actionType": "period", "subType": "start"},
                {"actionNumber": 694, "period": 1, "periodType": "OVERTIME", "clock": "02:41:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": "Tomas Pavelka"},
                {"actionNumber": 695, "period": 1, "periodType": "OVERTIME", "clock": "02:41:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": "Hendrik Eelmae"},
                {"actionNumber": 737, "period": 1, "periodType": "OVERTIME", "clock": "00:00:00", "actionType": "period", "subType": "end"},
            ],
        }
        substitutions = [event for event in data["pbp"] if event.get("actionType") == "substitution"]
        for event in substitutions:
            player_id = next(pid for pid, name in names.items() if name == event["player"])
            self.db.execute(
                "INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES('game-2155633',?,?,?,?,?,'substitution','success')",
                (event["actionNumber"], event["period"], event["clock"], 3, player_id),
            )
        self.db.commit()

        self.db.row_factory = sqlite3.Row
        game_row = self.db.execute(
            "SELECT g.game_id,g.home_team_id,g.away_team_id,s.json_url FROM games g JOIN source_livestats_games s USING(game_id) WHERE g.game_id='game-2155633'"
        ).fetchone()
        original_fetch = __import__("lineup_reconstructor").fetch_json
        reconstructor = __import__("lineup_reconstructor")
        reconstructor.fetch_json = lambda _: {**data, "pbp": list(reversed(data["pbp"]))}
        try:
            result = process_game(self.db, game_row, {names[pid]: str(pid) for pid in names}, {}, {})
        finally:
            reconstructor.fetch_json = original_fetch

        self.assertEqual(result["game_id"], "game-2155633")
        p2_segment = self.db.execute(
            "SELECT l.player_1,l.player_2,l.player_3,l.player_4,l.player_5 FROM lineup_segments s JOIN lineups l USING(lineup_id) WHERE s.game_id='game-2155633' AND s.period=2 AND s.start_clock='06:11:00' AND s.end_clock='02:56:00'"
        ).fetchone()
        self.assertEqual(set(map(str, p2_segment)), {"3", "4", "8", "11", "13"})
        overtime_segment = self.db.execute(
            "SELECT s.period,l.player_1,l.player_2,l.player_3,l.player_4,l.player_5 FROM lineup_segments s JOIN lineups l USING(lineup_id) WHERE s.game_id='game-2155633' AND s.period=5 ORDER BY s.start_action_number DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(overtime_segment)
        self.assertEqual(set(map(str, overtime_segment[1:])), {"1", "3", "4", "8", "11"})
        for row in self.db.execute("SELECT player_1,player_2,player_3,player_4,player_5 FROM lineups"):
            self.assertEqual(len(set(map(str, row))), 5)

    def test_failed_substitution_batch_leaves_previous_lineup_unchanged(self) -> None:
        lineup = {"1", "2", "3", "4", "5"}
        batch = [
            {"actionNumber": 10, "index": 1, "subType": "out", "player_id": "2"},
            {"actionNumber": 11, "index": 2, "subType": "in", "player_id": "6"},
            {"actionNumber": 12, "index": 3, "subType": "in", "player_id": "7"},
        ]

        with self.assertRaises(ValueError) as error:
            apply_substitution_batch(lineup, batch, "away", 2, "06:11:00", game_id="game-a")
        self.assertIn("lineup after batch has 6 players", str(error.exception))
        self.assertIn("actions=[10, 11, 12]", str(error.exception))

        self.assertEqual(lineup, {"1", "2", "3", "4", "5"})

    def test_outgoing_player_must_be_present_and_incoming_absent(self) -> None:
        for invalid_batch, reason in (
            ([{"actionNumber": 10, "index": 1, "subType": "out", "player_id": "9"},
              {"actionNumber": 11, "index": 2, "subType": "in", "player_id": "6"}], "outgoing players are not active"),
            ([{"actionNumber": 10, "index": 1, "subType": "out", "player_id": "2"},
              {"actionNumber": 11, "index": 2, "subType": "in", "player_id": "3"}], "incoming players are already active"),
        ):
            lineup = {"1", "2", "3", "4", "5"}
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                apply_substitution_batch(lineup, invalid_batch, "home", 1, "04:00:00")
            self.assertEqual(lineup, {"1", "2", "3", "4", "5"})

    def test_trace_and_debug_apply_same_clock_substitutions_atomically_by_logical_period(self) -> None:
        self.db.execute("INSERT INTO teams(team_id,canonical_name) VALUES(3,'Away team')")
        names = {1: "Away 1", 2: "Away 2", 3: "Away 3", 4: "Away 4", 5: "Away 5", 6: "Away 6", 7: "Away 7"}
        names.update({11: "Home 1", 12: "Home 2", 13: "Home 3", 14: "Home 4", 15: "Home 5"})
        self.db.execute(
            "INSERT INTO games(game_id,league_id,season_id,home_team_id,away_team_id,status) VALUES('diagnostic-game',1,1,2,3,'boxscore_imported')"
        )
        for player_id, name in names.items():
            if not self.db.execute("SELECT 1 FROM players WHERE player_id=?", (player_id,)).fetchone():
                self.db.execute("INSERT INTO players(player_id,canonical_name) VALUES(?,?)", (player_id, name))
            else:
                self.db.execute("UPDATE players SET canonical_name=? WHERE player_id=?", (name, player_id))
            team_id = 2 if player_id >= 11 else 3
            self.db.execute(
                "INSERT INTO player_games(game_id,player_id,team_id,player_name,team_name,shirt_number,starter) VALUES('diagnostic-game',?,?,?,?,?,?)",
                (player_id, team_id, name, "Home" if team_id == 2 else "Away", str(player_id), int(player_id in {1, 2, 3, 4, 5, 11, 12, 13, 14, 15})),
            )
        self.db.execute(
            "INSERT INTO source_livestats_games(game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES('diagnostic-game',1,1,'provider-diagnostic','diagnostic','mock://diagnostic')"
        )
        self.db.row_factory = sqlite3.Row
        events = [
            {"actionNumber": 10, "period": 1, "periodType": "REGULAR", "clock": "05:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": names[1]},
            {"actionNumber": 11, "period": 1, "periodType": "REGULAR", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": names[2]},
            {"actionNumber": 12, "period": 1, "periodType": "REGULAR", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": names[6]},
            {"actionNumber": 13, "period": 1, "periodType": "REGULAR", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": names[7]},
            {"actionNumber": 20, "period": 1, "periodType": "OVERTIME", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": names[6]},
            {"actionNumber": 21, "period": 1, "periodType": "OVERTIME", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "out", "player": names[7]},
            {"actionNumber": 22, "period": 1, "periodType": "OVERTIME", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": names[1]},
            {"actionNumber": 23, "period": 1, "periodType": "OVERTIME", "clock": "05:00:00", "tno": "2", "actionType": "substitution", "subType": "in", "player": names[2]},
        ]
        for event in events:
            player_id = next(player_id for player_id, name in names.items() if name == event["player"])
            self.db.execute(
                "INSERT INTO events(game_id,action_number,period,clock,team_id,player_id,event_type,event_result) VALUES('diagnostic-game',?,?,?,?,?,'substitution','success')",
                (event["actionNumber"], event["period"], event["clock"], 3, player_id),
            )
        self.db.commit()
        game_data = {
            "tm": {
                "1": {"name": "Home", "pl": {str(pid): {"name": names[pid], "starter": 1} for pid in range(11, 16)}},
                "2": {"name": "Away", "pl": {str(pid): {"name": names[pid], "starter": int(pid <= 5)} for pid in range(1, 8)}},
            },
            "pbp": events,
        }
        reconstructor = __import__("lineup_reconstructor")
        original_fetch = reconstructor.fetch_json
        reconstructor.fetch_json = lambda _: {**game_data, "pbp": list(reversed(game_data["pbp"]))}
        try:
            lookup = build_player_lookup(self.db)
            game_row = self.db.execute(
                "SELECT g.game_id,g.home_team_id,g.away_team_id,s.json_url FROM games g JOIN source_livestats_games s USING(game_id) WHERE g.game_id='diagnostic-game'"
            ).fetchone()
            self.assertIsNone(debug_first_invalid_event(self.db, game_row, lookup))
            from contextlib import redirect_stdout
            from io import StringIO

            output = StringIO()
            with redirect_stdout(output):
                run_trace_mode(self.db, "diagnostic-game")
        finally:
            reconstructor.fetch_json = original_fetch

        self.assertEqual(output.getvalue().count("TRACE_BATCH"), 2)
        self.assertIn("exact_explanation=no invalid lineup state encountered", output.getvalue())
        self.assertNotIn("REJECT_BATCH", output.getvalue())


if __name__ == "__main__":
    unittest.main()
