from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.identity.exceptions import ProviderError
from scripts.identity.models import IdentityContext, PlayerCandidate
from scripts.identity.providers.api_sports import ApiSportsProvider
from scripts.identity.providers.base import IdentityProvider
from scripts.identity.providers.basket import BasketProvider
from scripts.identity.resolver import IdentityResolver
from scripts.identity.resolve_livestats_players import _apply_configured_api_sports_mappings


class StaticProvider(IdentityProvider):
    def __init__(self, name: str, roster: tuple[PlayerCandidate, ...]) -> None:
        self.provider_name = name
        self.roster = roster
        self.calls: list[tuple[str, str]] = []

    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        self.calls.append((external_team_id, external_season))
        return self.roster


class ProviderAdapterTests(unittest.TestCase):
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
            (PlayerCandidate("basket", "05811", "Kristjan Kitsing", "Kristjan", "Kitsing"),),
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


if __name__ == "__main__":
    unittest.main()