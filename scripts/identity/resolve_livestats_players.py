"""JSON-lines bridge from the LiveStats Node importer to IdentityResolver.

The importer owns source parsing; this program owns no matching policy.  It
only turns parser-supplied occurrence contexts into resolver calls and returns
the resulting ARC player IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv

load_dotenv()

from scripts.identity.exceptions import ManualReviewRequired, ProviderError
from scripts.identity.models import IdentityContext
from scripts.identity.providers import ApiSportsProvider, BasketProvider
from scripts.identity.providers.roster_cache import RosterCache
from scripts.identity.resolver import IdentityResolver
from scripts.identity.utils import normalize_position

_LOGGER = logging.getLogger(__name__)
_API_SPORTS_MAPPINGS_PATH = Path(__file__).with_name("providers") / "api_sports_mappings.json"
_BASKET_MAPPINGS_PATH = Path(__file__).with_name("providers") / "basket_mappings.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("ARC_IDENTITY_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    payload = json.load(sys.stdin.buffer)
    occurrences = payload.get("occurrences")
    if not isinstance(occurrences, list):
        raise ValueError("Expected an object with an 'occurrences' list")

    source_connection = sqlite3.connect(
        f"{Path(args.database).resolve().as_uri()}?mode=ro", uri=True
    )
    connection = sqlite3.connect(":memory:")
    source_connection.backup(connection)
    source_connection.close()
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    _seed_additional_teams(connection, payload.get("additional_teams", []))
    before_identity = _snapshot_identity_tables(connection)
    basket_config = _load_basket_mapping_configuration()
    resolver = IdentityResolver(
        connection,
        providers=_configured_providers(),
        roster_cache=RosterCache(),
        provider_priority_by_league=_provider_priorities(basket_config),
    )
    resolved: dict[str, dict[str, object]] = {}
    identity_changes: dict[str, list[dict[str, object]]] = {}

    reviews: list[dict[str, object]] = []

    try:
        _apply_configured_api_sports_mappings(resolver, occurrences)
        _apply_configured_basket_mappings(resolver, occurrences, basket_config)
        for occurrence in occurrences:
            key = occurrence.get("key")
            raw_name = occurrence.get("raw_name")
            if not isinstance(key, str) or not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError("Each occurrence requires non-empty string key and raw_name")

            context = IdentityContext(
                raw_name=raw_name,
                team_id=_required_int(occurrence.get("team_id"), "team_id"),
                season_id=_required_int(occurrence.get("season_id"), "season_id"),
                league_id=_required_int(occurrence.get("league_id"), "league_id"),
                jersey_number=_optional_text(occurrence.get("jersey_number")),
                game_id=_optional_text(occurrence.get("game_id")),
                provider=_optional_text(occurrence.get("provider")) or "fiba_livestats",
                external_player_id=_optional_text(occurrence.get("external_player_id")),
                canonical_name_hint=_optional_text(occurrence.get("canonical_name_hint")),
                first_name=_optional_text(occurrence.get("first_name")),
                last_name=_optional_text(occurrence.get("last_name")),
                position=normalize_position(_optional_text(occurrence.get("position"))),
                ambiguous_source_name_group=occurrence.get("ambiguous_source_name_group") is True,
            )
            try:
                result = resolver.resolve(context)
            except ManualReviewRequired as exc:
                details = exc.details
                reviews.append(
                    {
                        "key": key,
                        "game_id": context.game_id,
                        "league_id": context.league_id,
                        "season_id": context.season_id,
                        "team_id": context.team_id,
                        "raw_name": context.raw_name,
                        "jersey_number": context.jersey_number,
                        "position": context.position,
                        "provider": context.provider,
                        "external_player_id": context.external_player_id,
                        "resolver_status": "review",
                        "confidence": None,
                        "candidate_players": details.get("candidate_players", _candidate_details(resolver, context)),
                        "candidate_information": details.get("candidate_information", []),
                        "reason": str(exc),
                    }
                )
                continue
            if result.player_id is None:
                raise ManualReviewRequired(f"No ARC player_id returned for {key!r}")
            resolved[key] = {
                "player_id": result.player_id,
                "status": result.status.value,
                "confidence": result.confidence,
            }
        if not reviews:
            identity_changes = _identity_table_changes(
                before_identity, _snapshot_identity_tables(connection)
            )
    finally:
        connection.close()

    # Return partial decisions as data. The caller must persist the source
    # occurrences and review outcomes even when this set cannot be activated.
    # Never include proposed canonical/alias writes for a review-bearing set.
    if reviews:
        known_player_ids = {
            str(row["player_id"]) for row in before_identity["players"]
        }
        occurrence_by_key = {item.get("key"): item for item in occurrences}
        for key, result in list(resolved.items()):
            if str(result.get("player_id")) in known_player_ids:
                continue
            occurrence = occurrence_by_key.get(key, {})
            reviews.append({
                "key": key,
                "game_id": occurrence.get("game_id"),
                "league_id": occurrence.get("league_id"),
                "season_id": occurrence.get("season_id"),
                "team_id": occurrence.get("team_id"),
                "raw_name": occurrence.get("raw_name"),
                "jersey_number": occurrence.get("jersey_number"),
                "position": occurrence.get("position"),
                "provider": occurrence.get("provider"),
                "external_player_id": occurrence.get("external_player_id"),
                "resolver_status": "review",
                "confidence": result.get("confidence"),
                "candidate_players": [],
                "candidate_information": [],
                "reason": "Player creation was withheld because another occurrence in this decision set requires review.",
            })
            del resolved[key]
        identity_changes = {}
    base_json = json.dumps(before_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    json.dump({"resolved": resolved, "reviews": reviews, "identity_changes": identity_changes,
               "identity_base_sha256": hashlib.sha256(base_json.encode("utf-8")).hexdigest()}, sys.stdout)
    return 0


_IDENTITY_TABLES = {
    "players": ("player_id",),
    "player_aliases": ("alias_id",),
    "player_external_ids": ("player_id", "provider", "external_player_id"),
    "team_external_ids": ("team_id", "provider"),
    "season_external_ids": ("season_id", "provider"),
    "league_external_ids": ("league_id", "provider"),
}


def _seed_additional_teams(
    connection: sqlite3.Connection, teams: object
) -> None:
    if not isinstance(teams, list):
        raise ValueError("additional_teams must be a list")
    for team in teams:
        if not isinstance(team, dict):
            raise ValueError("Each additional team must be an object")
        team_id = _required_int(team.get("team_id"), "team_id")
        name = _optional_text(team.get("canonical_name"))
        if not name:
            raise ValueError("Each additional team requires a canonical_name")
        connection.execute(
            "INSERT OR IGNORE INTO teams (team_id, canonical_name) VALUES (?, ?)",
            (team_id, name),
        )


def _snapshot_identity_tables(
    connection: sqlite3.Connection,
) -> dict[str, list[dict[str, object]]]:
    snapshot = {}
    for table in _IDENTITY_TABLES:
        columns = [row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        rows = [
            dict(row)
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')
        ]
        snapshot[table] = sorted(
            rows,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
        if not columns:
            raise ValueError(f"Identity table {table!r} is missing from database schema")
    return snapshot


def _identity_table_changes(
    before: dict[str, list[dict[str, object]]],
    after: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    changes: dict[str, list[dict[str, object]]] = {}
    for table, primary_key in _IDENTITY_TABLES.items():
        old_rows = {
            tuple(row[column] for column in primary_key): row
            for row in before[table]
        }
        changed = []
        for row in after[table]:
            key = tuple(row[column] for column in primary_key)
            if old_rows.get(key) != row:
                changed.append(row)
        changes[table] = changed
    return changes


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _required_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Each occurrence requires integer {field}")
    return value


def _configured_providers() -> tuple[object, ...]:
    configured = os.getenv("ARC_IDENTITY_PROVIDERS", "api_sports,basket")
    providers = []
    constructors = {
        "api_sports": ApiSportsProvider,
        "basket": BasketProvider,
    }

    for provider_name in (name.strip().lower() for name in configured.split(",")):
        constructor = constructors.get(provider_name)
        if constructor is None:
            continue
        try:
            providers.append(constructor())
        except ProviderError as exc:
            _LOGGER.warning("Identity provider %s disabled: %s", provider_name, exc)
            continue

    return tuple(providers)


def _apply_configured_api_sports_mappings(
    resolver: IdentityResolver, occurrences: list[dict[str, object]]
) -> None:
    """Persist explicitly configured API-Sports IDs before resolving players.

    ARC_API_SPORTS_MAPPINGS is JSON with ARC league/season IDs as keys and
    LiveStats source team IDs/codes as team keys, e.g. leagues, seasons, teams.
    """
    config = _load_api_sports_mapping_configuration()
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValueError("API-Sports mapping configuration must be a JSON object")

    league_mappings = _mapping_section(config, "leagues")
    season_mappings = _mapping_section(config, "seasons")

    applied_leagues: set[tuple[int, str]] = set()
    applied_seasons: set[tuple[int, str]] = set()
    applied_teams: set[tuple[int, str]] = set()

    for occurrence in occurrences:
        league_id = _required_int(occurrence.get("league_id"), "league_id")
        season_id = _required_int(occurrence.get("season_id"), "season_id")
        team_id = _required_int(occurrence.get("team_id"), "team_id")
        team_mappings = _team_mapping_section(config, league_id)

        external_league_id = league_mappings.get(str(league_id))
        if external_league_id:
            mapping = (league_id, external_league_id)
            if mapping not in applied_leagues:
                resolver.store_league_external_id(league_id, "api_sports", external_league_id)
                applied_leagues.add(mapping)

        external_season_id = season_mappings.get(str(season_id))
        if external_season_id:
            mapping = (season_id, external_season_id)
            if mapping not in applied_seasons:
                resolver.store_season_external_id(season_id, "api_sports", external_season_id)
                applied_seasons.add(mapping)

        source_team_id = _optional_text(occurrence.get("provider_team_external_id"))
        external_team_id = _lookup_team_mapping(team_mappings, source_team_id)
        if not external_team_id:
            external_team_id = team_mappings.get(str(team_id))
        if external_team_id:
            mapping = (team_id, external_team_id)
            if mapping not in applied_teams:
                resolver.store_team_external_id(team_id, "api_sports", external_team_id)
                applied_teams.add(mapping)


def _team_mapping_section(config: dict[str, object], league_id: int) -> dict[str, str]:
    section = config.get("teams", {})
    if not isinstance(section, dict):
        raise ValueError("API-Sports teams mapping must be a JSON object")
    league_section = section.get(str(league_id))
    has_league_scopes = any(isinstance(value, dict) for value in section.values())
    if isinstance(league_section, dict):
        return _mapping_section({"teams": league_section}, "teams")
    if has_league_scopes:
        if league_section is not None:
            raise ValueError(f"API-Sports teams mapping for league {league_id} must be an object")
        return {}
    return _mapping_section(config, "teams")


def _load_api_sports_mapping_configuration() -> dict[str, object] | None:
    if "ARC_API_SPORTS_MAPPINGS" in os.environ:
        raw_config = os.getenv("ARC_API_SPORTS_MAPPINGS", "").strip()
        if not raw_config:
            return None
        try:
            config = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ARC_API_SPORTS_MAPPINGS is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise ValueError("ARC_API_SPORTS_MAPPINGS must be a JSON object")
        return config

    try:
        with _API_SPORTS_MAPPINGS_PATH.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Could not load API-Sports mappings from {_API_SPORTS_MAPPINGS_PATH}: {exc}"
        ) from exc
    if not isinstance(config, dict):
        raise ValueError(f"{_API_SPORTS_MAPPINGS_PATH} must contain a JSON object")
    return config


def _mapping_section(config: dict[str, object], name: str) -> dict[str, str]:
    section = config.get(name, {})
    if not isinstance(section, dict):
        raise ValueError(f"ARC_API_SPORTS_MAPPINGS.{name} must be a JSON object")

    mappings: dict[str, str] = {}
    for source_id, external_id in section.items():
        if not isinstance(source_id, (str, int)) or isinstance(source_id, bool):
            raise ValueError(f"Invalid key in ARC_API_SPORTS_MAPPINGS.{name}")
        if not isinstance(external_id, (str, int)) or isinstance(external_id, bool):
            raise ValueError(f"Invalid provider ID in ARC_API_SPORTS_MAPPINGS.{name}")
        normalized_external_id = _optional_text(external_id)
        if not normalized_external_id:
            raise ValueError(f"Empty provider ID in ARC_API_SPORTS_MAPPINGS.{name}")
        mappings[str(source_id)] = normalized_external_id
    return mappings


def _load_basket_mapping_configuration() -> dict[str, object]:
    try:
        with _BASKET_MAPPINGS_PATH.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load Basket.ee mappings from {_BASKET_MAPPINGS_PATH}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{_BASKET_MAPPINGS_PATH} must contain a JSON object")
    return config


def _provider_priorities(config: dict[str, object]) -> dict[int, tuple[str, ...]]:
    raw_priorities = config.get("provider_order_by_arc_league", {})
    if not isinstance(raw_priorities, dict):
        raise ValueError("Basket provider_order_by_arc_league must be a JSON object")
    priorities: dict[int, tuple[str, ...]] = {}
    for league_id, order in raw_priorities.items():
        if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
            raise ValueError(f"Provider order for ARC league {league_id} must be a string array")
        priorities[_required_int(int(league_id), "league_id")] = tuple(order)
    return priorities


def _apply_configured_basket_mappings(
    resolver: IdentityResolver,
    occurrences: list[dict[str, object]],
    config: dict[str, object],
) -> None:
    league_scopes = _mapping_section(config, "league_scopes")
    season_mappings = _mapping_section(config, "seasons")
    team_sections = config.get("teams", {})
    if not isinstance(team_sections, dict):
        raise ValueError("Basket teams mappings must be a JSON object")

    for occurrence in occurrences:
        league_id = _required_int(occurrence.get("league_id"), "league_id")
        season_id = _required_int(occurrence.get("season_id"), "season_id")
        team_id = _required_int(occurrence.get("team_id"), "team_id")
        source_team_id = _optional_text(occurrence.get("provider_team_external_id"))
        season_mappings_for_league = team_sections.get(str(season_id), {})
        if not isinstance(season_mappings_for_league, dict):
            raise ValueError(f"Basket team mappings for ARC season {season_id} must be an object")
        external_team_id = _lookup_team_mapping(season_mappings_for_league, source_team_id)
        external_season_id = season_mappings.get(str(season_id))
        external_league_id = league_scopes.get(str(league_id))
        if external_team_id and external_season_id and external_league_id:
            resolver.configure_provider_roster_mapping(
                "basket",
                league_id,
                season_id,
                team_id,
                external_league_id,
                external_season_id,
                external_team_id,
            )


def _lookup_team_mapping(mappings: dict[str, str], source_team_id: str | None) -> str | None:
    if source_team_id is None:
        return None
    normalized = source_team_id.strip().casefold()
    return next(
        (external_id for source_id, external_id in mappings.items() if source_id.strip().casefold() == normalized),
        None,
    )


def _candidate_details(
    resolver: IdentityResolver, context: IdentityContext
) -> list[dict[str, object]]:
    candidates = resolver._search_candidates(context.raw_name, context)
    return [
        {
            "player_id": candidate["player_id"],
            "canonical_name": candidate["canonical_name"],
            "first_name": candidate["first_name"],
            "last_name": candidate["last_name"],
            "position": candidate["position"],
            "identity_status": candidate["identity_status"],
            "confidence": candidate["_score"],
        }
        for candidate in candidates
    ]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ManualReviewRequired, ValueError, sqlite3.Error) as exc:
        print(f"Identity resolution failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
