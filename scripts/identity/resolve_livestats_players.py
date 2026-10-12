"""JSON-lines bridge from the LiveStats Node importer to IdentityResolver.

The importer owns source parsing; this program owns no matching policy.  It
only turns parser-supplied occurrence contexts into resolver calls and returns
the resulting ARC player IDs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.identity.exceptions import ManualReviewRequired, ProviderError
from scripts.identity.models import IdentityContext
from scripts.identity.providers import ApiSportsProvider, BasketProvider
from scripts.identity.resolver import IdentityResolver
from scripts.identity.utils import normalize_position


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.load(sys.stdin)
    occurrences = payload.get("occurrences")
    if not isinstance(occurrences, list):
        raise ValueError("Expected an object with an 'occurrences' list")

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    resolver = IdentityResolver(connection, providers=_configured_providers())
    resolved: dict[str, dict[str, object]] = {}

    reviews: list[dict[str, object]] = []

    try:
        _apply_configured_api_sports_mappings(resolver, occurrences)
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
    finally:
        connection.close()

    if reviews:
        print(
            "ARC_IDENTITY_REVIEW " + json.dumps({"reviews": reviews}),
            file=sys.stderr,
        )
        return 2

    json.dump({"resolved": resolved}, sys.stdout)
    return 0


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
        except ProviderError:
            continue

    return tuple(providers)


def _apply_configured_api_sports_mappings(
    resolver: IdentityResolver, occurrences: list[dict[str, object]]
) -> None:
    """Persist explicitly configured API-Sports IDs before resolving players.

    ARC_API_SPORTS_MAPPINGS is JSON with ARC league/season IDs as keys and
    LiveStats source team IDs/codes as team keys, e.g. leagues, seasons, teams.
    """
    raw_config = os.getenv("ARC_API_SPORTS_MAPPINGS")
    if not raw_config:
        return

    try:
        config = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ARC_API_SPORTS_MAPPINGS is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("ARC_API_SPORTS_MAPPINGS must be a JSON object")

    league_mappings = _mapping_section(config, "leagues")
    season_mappings = _mapping_section(config, "seasons")
    team_mappings = _mapping_section(config, "teams")

    applied_leagues: set[tuple[int, str]] = set()
    applied_seasons: set[tuple[int, str]] = set()
    applied_teams: set[tuple[int, str]] = set()

    for occurrence in occurrences:
        league_id = _required_int(occurrence.get("league_id"), "league_id")
        season_id = _required_int(occurrence.get("season_id"), "season_id")
        team_id = _required_int(occurrence.get("team_id"), "team_id")

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
        external_team_id = team_mappings.get(source_team_id) if source_team_id else None
        if external_team_id:
            mapping = (team_id, external_team_id)
            if mapping not in applied_teams:
                resolver.store_team_external_id(team_id, "api_sports", external_team_id)
                applied_teams.add(mapping)


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
