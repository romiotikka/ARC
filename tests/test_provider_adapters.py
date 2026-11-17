from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.identity.exceptions import ManualReviewRequired, ProviderCoverageError, ProviderError
from scripts.identity.models import IdentityContext, PlayerCandidate
from scripts.identity.providers.api_sports import ApiSportsProvider
from scripts.identity.providers.base import IdentityProvider
from scripts.identity.providers.basket import BasketProvider
from scripts.identity.providers.roster_cache import RosterCache
from scripts.identity.resolver import IdentityResolver, _score_candidate_name
from scripts.identity.resolve_livestats_players import (
    _API_SPORTS_MAPPINGS_PATH,
    _apply_configured_basket_mappings,
    _apply_configured_api_sports_mappings,
    _load_basket_mapping_configuration,
    _load_api_sports_mapping_configuration,
    _provider_priorities,
)


class StaticProvider(IdentityProvider):
    def __init__(self, name: str, roster: tuple[PlayerCandidate, ...]) -> None:
        self.provider_name = name
        self.roster = roster
        self.calls: list[tuple[str, str]] = []
        self.call_order: list[str] | None = None

    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        self.calls.append((external_team_id, external_season))
        if self.call_order is not None:
            self.call_order.append(self.provider_name)
        return self.roster


class ProviderAdapterTests(unittest.TestCase):
    def _identity_database(self) -> sqlite3.Connection:
        database = sqlite3.connect(":memory:")
        database.executescript(
            (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text()
        )
        database.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'EstLatBL')")
        database.execute(
            "INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (20222023, '2022-23', 2022, 2023)"
        )
        database.execute("INSERT INTO teams (team_id, canonical_name) VALUES (4, 'VEF Riga')")
        database.commit()
        return database

    def _basket_priority_resolver(
        self,
        basket_roster: tuple[PlayerCandidate, ...],
        api_roster: tuple[PlayerCandidate, ...] = (),
    ) -> tuple[sqlite3.Connection, IdentityResolver, StaticProvider, StaticProvider]:
        database = self._identity_database()
        basket = StaticProvider("basket", basket_roster)
        api = StaticProvider("api_sports", api_roster)
        call_order: list[str] = []
        basket.call_order = call_order
        api.call_order = call_order
        resolver = IdentityResolver(
            database,
            providers=(api, basket),
            provider_priority_by_league={1: ("basket", "api_sports")},
        )
        resolver.configure_provider_roster_mapping(
            "basket", 1, 20222023, 4, "estlatbl", "2023", "7102"
        )
        resolver.store_league_external_id(1, "api_sports", "204")
        resolver.store_season_external_id(20222023, "api_sports", "2022")
        resolver.store_team_external_id(4, "api_sports", "1663")
        self._provider_call_order = call_order
        return database, resolver, basket, api

    def test_basket_mappings_are_season_scoped_and_resolved_from_live_team_code(self) -> None:
        database = self._identity_database()
        provider = StaticProvider("basket", ())
        resolver = IdentityResolver(database, providers=(provider,))
        config = _load_basket_mapping_configuration()
        self.assertEqual(_provider_priorities(config)[1], ("basket", "api_sports"))
        occurrence = {
            "league_id": 1,
            "season_id": 20222023,
            "team_id": 4,
            "provider_team_external_id": "VEF",
        }

        _apply_configured_basket_mappings(resolver, [occurrence], config)
        resolver._iter_provider_rosters(
            IdentityContext(raw_name="R. Berzins", team_id=4, season_id=20222023, league_id=1)
        )

        self.assertEqual(config["seasons"]["20222023"], "2023")
        self.assertEqual(config["teams"]["20222023"]["VEF"], "7102")
        self.assertEqual(provider.calls, [("7102", "2023")])
        self.assertEqual(
            database.execute("SELECT COUNT(*) FROM team_external_ids WHERE provider = 'basket'").fetchone()[0],
            0,
        )
        database.close()

    def test_valmiera_team_code_aliases_map_case_insensitively_for_both_providers(self) -> None:
        basket_config = _load_basket_mapping_configuration()
        for source_code in ("VIA", "Via", "VAL"):
            with self.subTest(source_code=source_code):
                database = self._identity_database()
                basket = StaticProvider("basket", ())
                api = StaticProvider("api_sports", ())
                resolver = IdentityResolver(database, providers=(basket, api))
                occurrence = {
                    "league_id": 1,
                    "season_id": 20222023,
                    "team_id": 4,
                    "provider_team_external_id": source_code,
                }

                with patch.dict("os.environ", {}, clear=True):
                    _apply_configured_basket_mappings(resolver, [occurrence], basket_config)
                    _apply_configured_api_sports_mappings(resolver, [occurrence])

                context = IdentityContext(
                    raw_name="E. Kaufmanis", team_id=4, season_id=20222023, league_id=1
                )
                resolver._iter_provider_rosters(context, providers=(basket,))
                resolver._iter_provider_rosters(context, providers=(api,))
                self.assertEqual(basket.calls, [("7101", "2023")])
                self.assertEqual(api.calls, [("2746", "2022")])
                database.close()

    def test_basket_is_primary_and_api_is_not_queried_when_basket_is_confident(self) -> None:
        basket_candidate = PlayerCandidate(
            "basket", "basket-roberts", "Roberts Berzins", "Roberts", "Berzins",
            jersey_number="5", position="G",
        )
        api_candidate = PlayerCandidate(
            "api_sports", "api-kaspars", "Kaspars Berzins", "Kaspars", "Berzins",
            jersey_number="24", position="F",
        )
        database, resolver, basket, api = self._basket_priority_resolver(
            (basket_candidate,), (api_candidate,)
        )

        result = resolver.resolve(
            IdentityContext(
                raw_name="R. Berzins", first_name="Roberts", last_name="Berzins",
                team_id=4, season_id=20222023, league_id=1,
                jersey_number="5", position="G", provider="fiba_livestats",
            )
        )

        self.assertEqual(basket.calls, [("7102", "2023")])
        self.assertEqual(api.calls, [])
        self.assertEqual(self._provider_call_order, ["basket"])
        self.assertEqual(result.status.value, "created")
        self.assertEqual(
            tuple(database.execute("SELECT canonical_name,first_name,last_name FROM players").fetchone()),
            ("Roberts Berzins", "Roberts", "Berzins"),
        )
        database.close()

    def test_basket_reversed_full_name_is_normalized_before_player_creation(self) -> None:
        candidate = PlayerCandidate(
            "basket", "basket-kitsing", "Kitsing, Kristjan", "Kristjan", "Kitsing",
            jersey_number="33", position="F",
        )
        database, resolver, basket, api = self._basket_priority_resolver((candidate,))

        result = resolver.resolve(
            IdentityContext(
                raw_name="K. Kitsing", team_id=4, season_id=20222023, league_id=1,
                jersey_number="33", position="F", provider="fiba_livestats",
            )
        )

        self.assertEqual(result.status.value, "created")
        self.assertEqual(
            tuple(database.execute("SELECT canonical_name,first_name,last_name FROM players").fetchone()),
            ("Kristjan Kitsing", "Kristjan", "Kitsing"),
        )
        self.assertEqual(api.calls, [])
        database.close()

    def test_unique_strong_full_name_can_resolve_without_jersey(self) -> None:
        candidate = PlayerCandidate(
            "basket", "no-jersey-id", "Oliver Metsalu", "Oliver", "Metsalu"
        )
        database, resolver, basket, api = self._basket_priority_resolver((candidate,))

        result = resolver.resolve(
            IdentityContext(
                raw_name="Metsalu Oliver", first_name="Oliver", last_name="Metsalu",
                team_id=4, season_id=20222023, league_id=1,
            )
        )

        self.assertEqual(result.status.value, "created")
        self.assertEqual(
            tuple(database.execute("SELECT canonical_name,first_name,last_name FROM players").fetchone()),
            ("Oliver Metsalu", "Oliver", "Metsalu"),
        )
        self.assertEqual(api.calls, [])
        database.close()

    def test_zunda_compound_surname_matches_exact_component_with_team_season_jersey(self) -> None:
        candidate = PlayerCandidate(
            "basket",
            "23317",
            "Kārlis Šilke-Žunda",
            "Kārlis",
            "Šilke-Žunda",
            jersey_number="13",
            position="G",
            birth_date="1997-07-25",
            height_cm=195,
            nationality="Läti",
        )
        database, resolver, basket, api = self._basket_priority_resolver((candidate,))

        result = resolver.resolve(
            IdentityContext(
                raw_name="K. Zunda",
                first_name="Karlis",
                last_name="Zunda",
                team_id=4,
                season_id=20222023,
                league_id=1,
                jersey_number="13",
                position="F",
                provider="fiba_livestats",
            )
        )

        self.assertEqual(result.status.value, "created")
        self.assertEqual(result.player_id, 1)
        self.assertEqual(
            tuple(database.execute("SELECT canonical_name,first_name,last_name,position FROM players").fetchone()),
            ("Kārlis Šilke-Žunda", "Kārlis", "Šilke-Žunda", "G"),
        )
        self.assertEqual(
            database.execute("SELECT external_player_id FROM player_external_ids WHERE provider='basket'").fetchone()[0],
            "23317",
        )
        self.assertEqual(api.calls, [])
        database.close()

    def test_reversed_full_name_matches_same_candidate_and_normalizes_player_order(self) -> None:
        candidate = PlayerCandidate(
            "basket", "reversed", "Metsalu Oliver", "Metsalu", "Oliver", jersey_number="6"
        )
        database, resolver, basket, api = self._basket_priority_resolver((candidate,))

        result = resolver.resolve(
            IdentityContext(
                raw_name="Oliver Metsalu", first_name="Oliver", last_name="Metsalu",
                team_id=4, season_id=20222023, league_id=1, jersey_number="6",
            )
        )

        self.assertEqual(result.status.value, "created")
        self.assertEqual(
            tuple(database.execute("SELECT canonical_name,first_name,last_name FROM players").fetchone()),
            ("Oliver Metsalu", "Oliver", "Metsalu"),
        )
        self.assertEqual(api.calls, [])
        database.close()

    def test_compound_surname_components_do_not_allow_unrelated_prefix_match(self) -> None:
        candidate = PlayerCandidate(
            "basket", "23317", "Kārlis Šilke-Žunda", "Kārlis", "Šilke-Žunda"
        )
        self.assertGreaterEqual(_score_candidate_name("Karlis Zunda", candidate), 0.90)
        self.assertEqual(_score_candidate_name("Karlis Zund", candidate), 0.0)
        self.assertLess(_score_candidate_name("Alice Zunda", candidate), 0.75)

    def test_two_compound_surname_candidates_with_same_jersey_require_review(self) -> None:
        roster = (
            PlayerCandidate("basket", "23317", "Kārlis Šilke-Žunda", "Kārlis", "Šilke-Žunda", jersey_number="13"),
            PlayerCandidate("basket", "second-zunda", "Kārlis Žunda", "Kārlis", "Žunda", jersey_number="13"),
        )
        database, resolver, basket, api = self._basket_priority_resolver(roster)

        with self.assertRaisesRegex(ManualReviewRequired, "Multiple provider roster candidates"):
            resolver.resolve(
                IdentityContext(
                    raw_name="K. Zunda", first_name="Karlis", last_name="Zunda",
                    team_id=4, season_id=20222023, league_id=1, jersey_number="13",
                )
            )

        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
        self.assertEqual(api.calls, [])
        database.close()

    def test_duplicate_strong_full_names_without_jersey_require_review(self) -> None:
        roster = (
            PlayerCandidate("basket", "oliver-1", "Oliver Metsalu", "Oliver", "Metsalu"),
            PlayerCandidate("basket", "oliver-2", "Metsalu Oliver", "Metsalu", "Oliver"),
        )
        database, resolver, basket, api = self._basket_priority_resolver(roster)

        with self.assertRaisesRegex(ManualReviewRequired, "Multiple provider roster candidates"):
            resolver.resolve(
                IdentityContext(
                    raw_name="Oliver Metsalu", first_name="Oliver", last_name="Metsalu",
                    team_id=4, season_id=20222023, league_id=1,
                )
            )

        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
        self.assertEqual(api.calls, [])
        database.close()

    def test_basket_jersey_number_disambiguates_similar_names_in_roster(self) -> None:
        roster = (
            PlayerCandidate("basket", "kristjan", "Kristjan Kitsing", "Kristjan", "Kitsing", jersey_number="33", position="F"),
            PlayerCandidate("basket", "kaspar", "Kaspar Kitsing", "Kaspar", "Kitsing", jersey_number="13", position="G"),
        )
        database, resolver, basket, api = self._basket_priority_resolver(roster)

        result = resolver.resolve(
            IdentityContext(
                raw_name="K. Kitsing", team_id=4, season_id=20222023, league_id=1,
                jersey_number="33", provider="fiba_livestats",
            )
        )

        self.assertEqual(result.status.value, "created")
        self.assertEqual(
            database.execute("SELECT canonical_name FROM players").fetchone()[0],
            "Kristjan Kitsing",
        )
        self.assertEqual(api.calls, [])
        database.close()

    def test_investigated_weak_local_candidates_do_not_block_basket_identity(self) -> None:
        cases = (
            ("P. Saal", "Patrik", "Saal", "33", "Patrik Saal", "15491", "Patrik Peemot", "Peemot", "P."),
            ("R. Aleksandrovs", "Rihards", "Aleksandrovs", "10", "Rihards Aleksandrovs", "25129", "Rihards Zebergs", "Zebergs", "R."),
            ("O. Metsalu", "Oliver", "Metsalu", "6", "Oliver Metsalu", "7574", "Oliver Suurorg", "Suurorg", "O."),
            ("R. Sats", "Ralf", "Sats", "48", "Ralf Sats", "16411", "Ralf Küttis", "Küttis", "R."),
        )
        for raw, first, last, jersey, provider_name, external_id, local_name, local_first, local_last in cases:
            with self.subTest(raw_name=raw):
                candidate = PlayerCandidate(
                    "basket", external_id, provider_name, first, last,
                    jersey_number=jersey, position="G",
                )
                database, resolver, basket, api = self._basket_priority_resolver((candidate,))
                database.execute(
                    "INSERT INTO players (player_id,canonical_name,first_name,last_name) VALUES (1,?,?,?)",
                    (local_name, local_first, local_last),
                )
                database.execute(
                    "INSERT INTO player_external_ids (player_id,provider,external_player_id) VALUES (1,'api_sports',?)",
                    (f"existing-{external_id}",),
                )
                database.commit()

                result = resolver.resolve(
                    IdentityContext(
                        raw_name=raw, first_name=first, last_name=last,
                        team_id=4, season_id=20222023, league_id=1,
                        jersey_number=jersey, position="G", provider="fiba_livestats",
                    )
                )

                self.assertEqual(result.status.value, "created")
                self.assertEqual(
                    database.execute("SELECT canonical_name FROM players WHERE player_id=?", (result.player_id,)).fetchone()[0],
                    provider_name,
                )
                self.assertEqual(
                    database.execute("SELECT canonical_name FROM players WHERE player_id=1").fetchone()[0],
                    local_name,
                )
                self.assertEqual(
                    database.execute("SELECT external_player_id FROM player_external_ids WHERE player_id=1").fetchone()[0],
                    f"existing-{external_id}",
                )
                self.assertEqual(api.calls, [])
                database.close()

    def test_two_strong_local_identities_still_require_review(self) -> None:
        database, resolver, basket, api = self._basket_priority_resolver((
            PlayerCandidate("basket", "provider-1", "Alex Smith", "Alex", "Smith", jersey_number="7"),
        ))
        for player_id in (1, 2):
            database.execute(
                "INSERT INTO players (player_id,canonical_name,first_name,last_name) VALUES (?, 'Alex Smith', 'Alex', 'Smith')",
                (player_id,),
            )
        database.commit()

        with self.assertRaisesRegex(ManualReviewRequired, "multiple ARC identities"):
            resolver.resolve(
                IdentityContext(
                    raw_name="Alex Smith", first_name="Alex", last_name="Smith",
                    team_id=4, season_id=20222023, league_id=1, jersey_number="7",
                )
            )

        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 2)
        self.assertEqual(api.calls, [])
        database.close()

    def test_same_team_season_jersey_history_keeps_weak_candidate_in_review(self) -> None:
        database, resolver, basket, api = self._basket_priority_resolver((
            PlayerCandidate("basket", "kaspar", "Kaspar Kitsing", "Kaspar", "Kitsing", jersey_number="13"),
        ))
        database.execute(
            "INSERT INTO players (player_id,canonical_name,first_name,last_name) VALUES (1,'Kristjan Kitsing','Kristjan','Kitsing')"
        )
        database.execute(
            "INSERT INTO games (game_id,league_id,season_id,status) VALUES ('history',1,20222023,'boxscore_imported')"
        )
        database.execute(
            "INSERT INTO player_games (game_id,player_id,team_id,player_name,team_name,shirt_number) VALUES ('history',1,4,'Kristjan Kitsing','VEF Riga','13')"
        )
        database.commit()

        with self.assertRaisesRegex(ManualReviewRequired, "context-supported identity"):
            resolver.resolve(
                IdentityContext(
                    raw_name="K. Kitsing", first_name="Kaspar", last_name="Kitsing",
                    team_id=4, season_id=20222023, league_id=1, jersey_number="13",
                )
            )

        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 1)
        self.assertEqual(api.calls, [])
        database.close()

    def test_second_batch_weak_local_candidates_do_not_block_basket_full_names(self) -> None:
        cases = (
            ("P. Saal", "Patrik", "Saal", "33", "Patrik Saal", "Patrik Peemot", "Peemot", "P."),
            ("R. Aleksandrovs", "Rihards", "Aleksandrovs", "10", "Rihards Aleksandrovs", "Rihards Zebergs", "Zebergs", "R."),
            ("O. Metsalu", "Oliver", "Metsalu", "6", "Oliver Metsalu", "Oliver Suurorg", "Suurorg", "O."),
            ("R. Sats", "Ralf", "Sats", "48", "Ralf Sats", "Ralf Küttis", "Küttis", "R."),
        )
        for raw, first, last, jersey, provider_name, local_name, local_first, local_last in cases:
            with self.subTest(raw_name=raw):
                candidate = PlayerCandidate(
                    "basket", f"basket-{last}", provider_name, first, last,
                    jersey_number=jersey, position="G",
                )
                database, resolver, basket, api = self._basket_priority_resolver((candidate,))
                database.execute(
                    "INSERT INTO players (player_id,canonical_name,first_name,last_name) VALUES (1,?,?,?)",
                    (local_name, local_first, local_last),
                )
                database.execute(
                    "INSERT INTO player_external_ids (player_id,provider,external_player_id) VALUES (1,'api_sports',?)",
                    (f"existing-{last}",),
                )
                database.execute(
                    "INSERT INTO player_aliases (player_id,alias_name,source) VALUES (1,?, 'manual')",
                    (f"alias-{last}",),
                )
                database.commit()

                result = resolver.resolve(
                    IdentityContext(
                        raw_name=raw, first_name=first, last_name=last,
                        team_id=4, season_id=20222023, league_id=1,
                        jersey_number=jersey, position="G", provider="fiba_livestats",
                    )
                )

                self.assertEqual(result.status.value, "created")
                self.assertEqual(
                    database.execute("SELECT canonical_name FROM players WHERE player_id=?", (result.player_id,)).fetchone()[0],
                    provider_name,
                )
                self.assertEqual(
                    database.execute("SELECT canonical_name FROM players WHERE player_id=1").fetchone()[0],
                    local_name,
                )
                self.assertEqual(
                    database.execute("SELECT external_player_id FROM player_external_ids WHERE player_id=1").fetchone()[0],
                    f"existing-{last}",
                )
                self.assertEqual(
                    database.execute("SELECT alias_name FROM player_aliases WHERE player_id=1").fetchone()[0],
                    f"alias-{last}",
                )
                self.assertEqual(api.calls, [])
                database.close()

    def test_api_initial_without_full_name_or_jersey_is_reviewed(self) -> None:
        candidate = PlayerCandidate("api_sports", "zunda", "Zunda K.", "K.", "Zunda")
        database, resolver, basket, api = self._basket_priority_resolver((), (candidate,))

        with self.assertRaisesRegex(ManualReviewRequired, "Ambiguous provider roster"):
            resolver.resolve(
                IdentityContext(
                    raw_name="K. Zunda", first_name="Karlis", last_name="Zunda",
                    team_id=4, season_id=20222023, league_id=1, jersey_number="13",
                )
            )

        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
        self.assertEqual(basket.calls, [("7102", "2023")])
        self.assertEqual(api.calls, [("1663", "2022")])
        database.close()

    def test_api_initial_without_jersey_is_not_confident_enough_to_match(self) -> None:
        candidate = PlayerCandidate("api_sports", "zunda", "Zunda K.", "K.", "Zunda")
        database, resolver, basket, api = self._basket_priority_resolver((), (candidate,))

        with self.assertRaisesRegex(ManualReviewRequired, "Ambiguous provider roster"):
            resolver.resolve(
                IdentityContext(
                    raw_name="K. Zunda", first_name="Karlis", last_name="Zunda",
                    team_id=4, season_id=20222023, league_id=1, jersey_number="13",
                )
            )

        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
        self.assertEqual(basket.calls, [("7102", "2023")])
        self.assertEqual(api.calls, [("1663", "2022")])
        database.close()

    def test_basket_name_and_conflicting_jersey_require_review(self) -> None:
        roster = (
            PlayerCandidate("basket", "kristjan", "Kristjan Kitsing", "Kristjan", "Kitsing", jersey_number="33", position="F"),
            PlayerCandidate("basket", "kaspar", "Kaspar Kitsing", "Kaspar", "Kitsing", jersey_number="13", position="G"),
        )
        database, resolver, basket, api = self._basket_priority_resolver(roster)

        with self.assertRaisesRegex(ManualReviewRequired, "jersey number"):
            resolver.resolve(
                IdentityContext(
                    raw_name="Kristjan Kitsing", first_name="Kristjan", last_name="Kitsing",
                    team_id=4, season_id=20222023, league_id=1,
                    jersey_number="13", provider="fiba_livestats",
                )
            )

        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
        self.assertEqual(api.calls, [])
        database.close()

    def test_api_sports_is_fallback_after_empty_basket_roster(self) -> None:
        api_candidate = PlayerCandidate(
            "api_sports", "7177", "Roberts Berzins", "Roberts", "Berzins",
            jersey_number="5", position="G",
        )
        database, resolver, basket, api = self._basket_priority_resolver((), (api_candidate,))

        result = resolver.resolve(
            IdentityContext(
                raw_name="R. Berzins", first_name="Roberts", last_name="Berzins",
                team_id=4, season_id=20222023, league_id=1,
                jersey_number="5", position="G", provider="fiba_livestats",
            )
        )

        self.assertEqual(basket.calls, [("7102", "2023")])
        self.assertEqual(api.calls, [("1663", "2022")])
        self.assertEqual(self._provider_call_order, ["basket", "api_sports"])
        self.assertEqual(result.status.value, "created")
        self.assertEqual(
            database.execute("SELECT canonical_name FROM players").fetchone()[0],
            "Roberts Berzins",
        )
        database.close()

    def test_api_fallback_can_resolve_when_basket_has_only_weak_name_evidence(self) -> None:
        basket_candidate = PlayerCandidate(
            "basket", "ojars", "Ojars Berzins", "Ojars", "Berzins", jersey_number="27"
        )
        api_candidate = PlayerCandidate(
            "api_sports", "7177", "Roberts Berzins", "Roberts", "Berzins",
            jersey_number="5", position="G",
        )
        database, resolver, basket, api = self._basket_priority_resolver(
            (basket_candidate,), (api_candidate,)
        )

        result = resolver.resolve(
            IdentityContext(
                raw_name="R. Berzins", first_name="Roberts", last_name="Berzins",
                team_id=4, season_id=20222023, league_id=1,
                jersey_number="5", position="G", provider="fiba_livestats",
            )
        )

        self.assertEqual(self._provider_call_order, ["basket", "api_sports"])
        self.assertEqual(result.status.value, "created")
        self.assertEqual(
            database.execute("SELECT canonical_name FROM players").fetchone()[0],
            "Roberts Berzins",
        )
        database.close()

    def test_existing_confirmed_identity_and_alias_are_preserved(self) -> None:
        database, resolver, basket, api = self._basket_priority_resolver(())
        database.execute(
            "INSERT INTO players (player_id,canonical_name,first_name,last_name,identity_status) VALUES (17,'Kristjan Kitsing','Kristjan','Kitsing','verified')"
        )
        database.execute(
            "INSERT INTO player_aliases (player_id,alias_name,source) VALUES (17,'K. Kitsing','manual')"
        )
        database.execute(
            "INSERT INTO player_external_ids (player_id,provider,external_player_id) VALUES (17,'fiba_livestats','live-42')"
        )
        database.commit()

        result = resolver.resolve(
            IdentityContext(
                raw_name="K. Kitsing", team_id=4, season_id=20222023, league_id=1,
                external_player_id="live-42", provider="fiba_livestats",
            )
        )

        self.assertEqual(result.player_id, 17)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 1)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM player_aliases WHERE player_id=17").fetchone()[0], 1)
        self.assertEqual(database.execute("SELECT identity_status FROM players WHERE player_id=17").fetchone()[0], "verified")
        self.assertEqual(basket.calls, [])
        self.assertEqual(api.calls, [])
        database.close()

    def test_api_sports_plan_coverage_is_a_distinct_provider_error(self) -> None:
        provider = object.__new__(ApiSportsProvider)
        with self.assertRaises(ProviderCoverageError):
            provider._validate_envelope(
                {"errors": {"plan": "season unavailable"}, "response": []},
                "/players",
            )
        with self.assertRaises(ProviderError) as error:
            provider._validate_envelope(
                {"errors": {"search": "invalid query"}, "response": []},
                "/players",
            )
        self.assertNotIsInstance(error.exception, ProviderCoverageError)

    def test_persistent_mappings_expose_roberts_berzins_in_resolver_path(self) -> None:
        database = self._identity_database()
        provider = StaticProvider(
            "api_sports",
            (
                PlayerCandidate(
                    "api_sports",
                    "7177",
                    "Roberts Berzins",
                    "Roberts",
                    "Berzins",
                    jersey_number="55",
                    nationality="Latvia",
                    position="G",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as cache_dir:
            resolver = IdentityResolver(
                database,
                providers=(provider,),
                roster_cache=RosterCache(Path(cache_dir)),
            )
            occurrence = {
                "league_id": 1,
                "season_id": 20222023,
                "team_id": 4,
                "provider_team_external_id": "VEF",
            }
            with patch.dict("os.environ", {}, clear=True):
                config = _load_api_sports_mapping_configuration()
                self.assertEqual(config["leagues"]["1"], "204")
                self.assertEqual(
                    config["seasons"],
                    {
                        "20222023": "2022",
                        "20232024": "2023",
                        "20242025": "2024",
                        "20252026": "2025",
                    },
                )
                self.assertEqual(config["teams"]["1"]["VEF"], "1663")
                self.assertEqual(
                    config["coverage"]["seasons"]["2025"]["roster_endpoint"]["status"],
                    "plan_restricted",
                )
                _apply_configured_api_sports_mappings(resolver, [occurrence])

            with self.assertRaisesRegex(ManualReviewRequired, "jersey number"):
                resolver.resolve(
                    IdentityContext(
                        raw_name="R. Berzins",
                        first_name="Roberts",
                        last_name="Berzins",
                        team_id=4,
                        season_id=20222023,
                        league_id=1,
                        jersey_number="5",
                        position="G",
                        game_id="estlatbl_2023212002",
                        provider="fiba_livestats",
                    )
                )

        self.assertEqual(provider.calls, [("1663", "2022")])
        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM player_external_ids").fetchone()[0], 0)
        self.assertEqual(
            database.execute(
                "SELECT external_team_id FROM team_external_ids WHERE provider = 'api_sports'"
            ).fetchone()[0],
            "1663",
        )
        database.close()

    def test_roster_cache_is_persistent_and_keyed_by_full_context(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = RosterCache(Path(cache_dir))
            candidate = PlayerCandidate("api_sports", "p1", "Alpha One", "Alpha", "One")
            cache.put("api_sports", "204", "2022", "1663", "success", (candidate,))

            hit = cache.get("api_sports", "204", "2022", "1663")
            self.assertIsNotNone(hit)
            self.assertEqual(hit.status, "success")
            self.assertEqual(hit.roster[0].canonical_name, "Alpha One")
            self.assertIsNone(cache.get("api_sports", "204", "2023", "1663"))
            self.assertIsNone(cache.get("api_sports", "204", "2022", "2748"))

            cache.put("api_sports", "204", "2022", "2748", "empty")
            cache.put("api_sports", "204", "2024", "1663", "unsupported", error="plan")
            cache.put("api_sports", "204", "2023", "1663", "error", error="timeout")
            self.assertEqual(cache.get("api_sports", "204", "2022", "2748").status, "empty")
            self.assertEqual(cache.get("api_sports", "204", "2024", "1663").status, "unsupported")
            self.assertEqual(cache.get("api_sports", "204", "2023", "1663").status, "error")

    def test_resolver_reuses_roster_cache_across_instances(self) -> None:
        database = self._identity_database()
        roster = (
            PlayerCandidate("api_sports", "p1", "Alpha One", "Alpha", "One", jersey_number="1"),
            PlayerCandidate("api_sports", "p2", "Beta Two", "Beta", "Two", jersey_number="2"),
        )
        first_provider = StaticProvider("api_sports", roster)
        second_provider = StaticProvider("api_sports", roster)
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = Path(cache_dir)
            first = IdentityResolver(
                database,
                providers=(first_provider,),
                roster_cache=RosterCache(cache_path),
            )
            for resolver in (first,):
                resolver.store_league_external_id(1, "api_sports", "204")
                resolver.store_season_external_id(20222023, "api_sports", "2022")
                resolver.store_team_external_id(4, "api_sports", "1663")
            first.resolve(
                IdentityContext(
                    raw_name="Alpha One", team_id=4, season_id=20222023, league_id=1, jersey_number="1"
                )
            )

            second = IdentityResolver(
                database,
                providers=(second_provider,),
                roster_cache=RosterCache(cache_path),
            )
            second.resolve(
                IdentityContext(
                    raw_name="Beta Two", team_id=4, season_id=20222023, league_id=1, jersey_number="2"
                )
            )

        self.assertEqual(first_provider.calls, [("1663", "2022")])
        self.assertEqual(second_provider.calls, [])
        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 2)
        database.close()

    def test_missing_provider_mappings_are_logged_and_not_fetched(self) -> None:
        database = self._identity_database()
        provider = StaticProvider("api_sports", ())
        resolver = IdentityResolver(database, providers=(provider,))
        context = IdentityContext(
            raw_name="R. Berzins", team_id=4, season_id=20222023, league_id=1
        )
        with self.assertLogs("scripts.identity.resolver", level="WARNING") as captured:
            self.assertEqual(resolver._iter_provider_rosters(context), ())
        self.assertIn("missing=league,season,team", captured.output[0])
        self.assertEqual(provider.calls, [])
        database.close()

    def test_api_sports_player_and_team_parsing(self) -> None:
        provider = object.__new__(ApiSportsProvider)
        players = provider._parse_player_response(
            {
                "response": [
                    {
                        "id": 45678,
                        "firstname": "Kristjan",
                        "lastname": "Kitsing",
                        "birth": {"date": "1990-12-15"},
                        "nationality": "Estonian",
                        "height": {"meters": "2.04"},
                        "leagues": {"1": {"pos": "PF", "jersey": 7}},
                    }
                ]
            }
        )
        self.assertEqual(players[0].external_player_id, "45678")
        self.assertEqual(players[0].canonical_name, "Kristjan Kitsing")
        self.assertEqual(players[0].height_cm, 204)
        self.assertEqual(players[0].position, "F")
        self.assertEqual(players[0].jersey_number, "7")

        compact = provider._parse_player_response(
            {"response": [{"id": 2383, "name": "Kitsing Kristjan", "number": "33", "country": "Estonia", "position": "Forward"}]}
        )[0]
        self.assertEqual(compact.external_player_id, "2383")
        self.assertEqual(compact.canonical_name, "Kristjan Kitsing")
        self.assertEqual(compact.nationality, "Estonia")
        self.assertEqual(compact.position, "F")
        self.assertEqual(compact.jersey_number, "33")

        teams = provider._parse_teams_response(
            {"response": [{"id": 987, "name": "TalTech/ALEXELA", "code": "TCH"}]}
        )
        self.assertEqual(teams[0].team_id, 987)
        self.assertEqual(teams[0].name, "TalTech/ALEXELA")

    def test_api_sports_missing_player_id_is_provider_error(self) -> None:
        provider = object.__new__(ApiSportsProvider)
        with self.assertRaises(ProviderError):
            provider._parse_player_response({"response": [{"firstname": "Unknown"}]})

    def test_basket_roster_and_profile_parsing(self) -> None:
        roster_url = "https://fixture.test/et/osalevad-voistkonnad/team-1//roster?setSid=2026"
        profile_url = "https://fixture.test/et/mangijad?pid=05811"
        fixtures = {
            roster_url: '<a href="https://fixture.test/et/mangijad?pid=05811">Kitsing, Kristjan</a>',
            profile_url: """
                <html><h1>Kristjan Kitsing</h1>
                <div>Pikkus: 204 cm Kaal: 98 kg Sünniaeg: 15.12.1990 Riik: Eesti</div>
                <div>Power forward (PF)</div></html>
            """,
        }
        provider = BasketProvider(
            base_url="https://fixture.test",
            fetch_text=lambda url: fixtures[url],
        )

        candidates = provider.get_season_roster("team-1", "2026")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].provider, "basket")
        self.assertEqual(candidates[0].external_player_id, "05811")
        self.assertEqual(candidates[0].canonical_name, "Kristjan Kitsing")
        self.assertEqual(candidates[0].first_name, "Kristjan")
        self.assertEqual(candidates[0].last_name, "Kitsing")
        self.assertEqual(candidates[0].birth_date, "1990-12-15")
        self.assertEqual(candidates[0].height_cm, 204)
        self.assertEqual(candidates[0].nationality, "Eesti")
        self.assertEqual(candidates[0].position, "F")

    def test_basket_roster_preserves_jersey_and_roster_position(self) -> None:
        roster_url = "https://fixture.test/et/osalevad-voistkonnad/7102//roster?setSid=2023"
        profile_url = "https://fixture.test/et/mangijad?pid=21898"
        fixtures = {
            roster_url: """
                <div class="player" onclick="document.location='https://fixture.test/et/mangijad?pid=21898'">
                  <div class="photo"></div>
                  <div class="name">#5 Roberts Berzins</div>
                  <div class="positionAndHeight">
                    <span>Viskav tagamängija (SG)</span><span class="height">1.93</span>
                  </div>
                </div>
            """,
            profile_url: "<html><h1>Roberts Berzins</h1><div>Sünniaeg: 12.02.2001 Riik: Läti</div></html>",
        }
        provider = BasketProvider(
            base_url="https://fixture.test",
            fetch_text=lambda url: fixtures[url],
        )

        candidates = provider.get_season_roster("7102", "2023")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].canonical_name, "Roberts Berzins")
        self.assertEqual(candidates[0].first_name, "Roberts")
        self.assertEqual(candidates[0].last_name, "Berzins")
        self.assertEqual(candidates[0].jersey_number, "5")
        self.assertEqual(candidates[0].position, "G")

    def test_basket_search_no_result_and_provider_error(self) -> None:
        provider = BasketProvider(
            base_url="https://fixture.test",
            fetch_text=lambda url: "<html><body>No players</body></html>",
        )
        self.assertEqual(provider.search_players("nobody"), ())

        failing = BasketProvider(
            fetch_text=lambda url: (_ for _ in ()).throw(OSError("offline")),
        )
        with self.assertRaises(ProviderError):
            failing.search_players("Kitsing")

    def test_cross_provider_ids_share_one_arc_player(self) -> None:
        database = sqlite3.connect(":memory:")
        database.executescript(
            (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text()
        )
        database.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'Test')")
        database.execute("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (3, 'Test', 2025, 2026)")
        database.execute("INSERT INTO teams (team_id, canonical_name) VALUES (1, 'Test Team')")
        database.commit()

        resolver = IdentityResolver(database)
        created = resolver.resolve(
            IdentityContext(
                raw_name="Kristjan Kitsing",
                team_id=1,
                season_id=3,
                league_id=1,
                provider="fiba_livestats",
            )
        )
        self.assertEqual(created.player_id, 1)
        resolver.store_external_id(1, "api_sports", "45678")

        basket = StaticProvider(
            "basket",
            (PlayerCandidate("basket", "05811", "Kristjan Kitsing", "Kristjan", "Kitsing", jersey_number="7"),),
        )
        resolver.store_team_external_id(1, "basket", "team-1")
        resolver.store_season_external_id(3, "basket", "2026")
        resolver.store_league_external_id(1, "basket", "league-1")
        resolver_with_basket = IdentityResolver(database, providers=(basket,))
        matched = resolver_with_basket.resolve(
            IdentityContext(
                raw_name="K. Kitsing",
                team_id=1,
                season_id=3,
                league_id=1,
                jersey_number="7",
                provider="fiba_livestats",
            )
        )

        self.assertEqual(matched.player_id, 1)
        mappings = [tuple(row) for row in database.execute(
            "SELECT provider, external_player_id FROM player_external_ids ORDER BY provider"
        ).fetchall()]
        self.assertEqual(mappings, [("api_sports", "45678"), ("basket", "05811")])
        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 1)
        database.close()

    def test_configured_api_mappings_enable_existing_provider_resolution(self) -> None:
        database = sqlite3.connect(":memory:")
        database.executescript(
            (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text()
        )
        database.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'EstLatBL')")
        database.execute("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (20252026, '2025-26', 2025, 2026)")
        database.execute("INSERT INTO teams (team_id, canonical_name) VALUES (1, 'TalTech/ALEXELA')")
        database.execute(
            "INSERT INTO players (player_id, canonical_name, first_name, last_name) VALUES (17, 'Kristjan Kitsing', 'Kristjan', 'Kitsing')"
        )
        database.commit()

        provider = StaticProvider(
            "api_sports",
            (
                PlayerCandidate(
                    "api_sports",
                    "api-player-456",
                    "Kristjan Kitsing",
                    "Kristjan",
                    "Kitsing",
                    jersey_number="7",
                    birth_date="1990-12-15",
                    nationality="Estonian",
                    height_cm=204,
                    position="F",
                ),
            ),
        )
        resolver = IdentityResolver(database, providers=(provider,))
        occurrences = [{
            "league_id": 1,
            "season_id": 20252026,
            "team_id": 1,
            "provider_team_external_id": "TCH",
        }]

        with patch.dict(
            "os.environ",
            {
                "ARC_API_SPORTS_MAPPINGS": (
                    '{"leagues":{"1":"204"},"seasons":{"20252026":"2025"},'
                    '"teams":{"TCH":"483"}}'
                )
            },
        ):
            _apply_configured_api_sports_mappings(resolver, occurrences)

        result = resolver.resolve(
            IdentityContext(
                raw_name="K. Kitsing",
                team_id=1,
                season_id=20252026,
                league_id=1,
                jersey_number="7",
                provider="fiba_livestats",
            )
        )

        self.assertEqual(provider.calls, [("483", "2025")])
        self.assertEqual(result.player_id, 17)
        self.assertNotEqual(result.player_id, "api-player-456")
        self.assertEqual(
            database.execute("SELECT external_player_id FROM player_external_ids WHERE provider = 'api_sports'").fetchone()[0],
            "api-player-456",
        )
        self.assertEqual(
            database.execute("SELECT external_league_id FROM league_external_ids WHERE provider = 'api_sports'").fetchone()[0],
            "204",
        )
        self.assertEqual(
            database.execute("SELECT external_season_id FROM season_external_ids WHERE provider = 'api_sports'").fetchone()[0],
            "2025",
        )
        self.assertEqual(
            database.execute("SELECT external_team_id FROM team_external_ids WHERE provider = 'api_sports'").fetchone()[0],
            "483",
        )
        self.assertEqual(
            tuple(database.execute("SELECT birth_date, nationality, height_cm, position FROM players WHERE player_id = 17").fetchone()),
            ("1990-12-15", "Estonian", 204, "F"),
        )
        database.close()

    def test_configured_api_mappings_require_integer_arc_ids(self) -> None:
        database = sqlite3.connect(":memory:")
        resolver = IdentityResolver(database)
        try:
            with patch.dict(
                "os.environ",
                {"ARC_API_SPORTS_MAPPINGS": '{"leagues":{},"seasons":{},"teams":{}}'},
            ):
                with self.assertRaisesRegex(ValueError, "integer league_id"):
                    _apply_configured_api_sports_mappings(
                        resolver,
                        [{"league_id": "1", "season_id": 20252026, "team_id": 1}],
                    )
        finally:
            database.close()

    def test_configured_api_mappings_support_arc_team_id_key(self) -> None:
        database = sqlite3.connect(":memory:")
        database.executescript(
            (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text()
        )
        database.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'EstLatBL')")
        database.execute("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (20252026, '2025-26', 2025, 2026)")
        database.execute("INSERT INTO teams (team_id, canonical_name) VALUES (1, 'TalTech/ALEXELA')")
        database.commit()

        resolver = IdentityResolver(database)
        occurrences = [{
            "league_id": 1,
            "season_id": 20252026,
            "team_id": 1,
            "provider_team_external_id": None,
        }]

        with patch.dict(
            "os.environ",
            {
                "ARC_API_SPORTS_MAPPINGS": (
                    '{"leagues":{"1":"204"},"seasons":{"20252026":"2025"},'
                    '"teams":{"1":"483"}}'
                )
            },
        ):
            _apply_configured_api_sports_mappings(resolver, occurrences)

        self.assertEqual(
            database.execute("SELECT external_team_id FROM team_external_ids WHERE provider = 'api_sports'").fetchone()[0],
            "483",
        )
        database.close()

    def test_resolver_with_providers_reviews_initial_only_local_match(self) -> None:
        database = sqlite3.connect(":memory:")
        database.executescript(
            (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text()
        )
        database.execute("INSERT INTO leagues (league_id, name) VALUES (1, 'EstLatBL')")
        database.execute("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (20252026, '2025-26', 2025, 2026)")
        database.execute("INSERT INTO teams (team_id, canonical_name) VALUES (1, 'VEF Riga')")
        database.execute(
            "INSERT INTO players (player_id, canonical_name, first_name, last_name) VALUES (14, 'Artis Ate', 'Artis', 'Ate')"
        )
        database.commit()

        provider = StaticProvider("api_sports", ())
        resolver = IdentityResolver(database, providers=(provider,))

        with self.assertRaisesRegex(ManualReviewRequired, "Ambiguous identity"):
            resolver.resolve(
                IdentityContext(
                    raw_name="A. Ate",
                    team_id=1,
                    season_id=20252026,
                    league_id=1,
                    provider="fiba_livestats",
                )
            )
        self.assertEqual(database.execute("SELECT COUNT(*) FROM players").fetchone()[0], 1)
        database.close()


if __name__ == "__main__":
    unittest.main()