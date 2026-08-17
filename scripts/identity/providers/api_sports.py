from __future__ import annotations

import http.client
import json
import os
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv

from ..exceptions import ProviderError
from ..models import PlayerCandidate, TeamInfo
from .base import IdentityProvider


class ApiSportsProvider(IdentityProvider):
    """Primary Identity v2 provider — thin HTTP adapter over API-Sports basketball.

    Responsibilities:
    - Authenticate and communicate with API-Sports.
    - Convert API JSON into normalized :class:`~scripts.identity.models.PlayerCandidate` objects.
    - Raise :class:`~scripts.identity.exceptions.ProviderError` on any failure.

    Non-responsibilities (all belong to IdentityResolver):
    - Identity resolution, player matching, confidence scoring.
    - Database access or player_id generation.
    - Alias generation or canonical name comparison.
    """

    provider_name = "api_sports"

    # ── API constants ─────────────────────────────────────────────────────────

    _HOST = "v1.basketball.api-sports.io"
    _PLAYERS_ENDPOINT = "/players"
    _TEAMS_ENDPOINT = "/teams"
    _REQUEST_TIMEOUT = 30  # seconds

    # ── Position normalization map (raw API value → ARC group token) ──────────

    _POSITION_MAP: dict[str, str] = {
        # Abbreviated codes
        "pg": "G", "sg": "G", "g": "G",
        "sf": "F", "pf": "F", "f": "F",
        "c": "C",
        # Word form
        "guard": "G", "point guard": "G", "shooting guard": "G",
        "forward": "F", "small forward": "F", "power forward": "F",
        "center": "C",
        # Numeric (FIBA-style)
        "1": "G", "2": "G",
        "3": "F", "4": "F",
        "5": "C",
        # Already-normalized ARC combos (pass through)
        "g-f": "G-F", "f-c": "F-C",
    }

    _POSITION_ORDER = ("G", "F", "C")

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(self) -> None:
        load_dotenv()
        api_key = os.getenv("API_SPORTS_KEY")
        if not api_key:
            raise ProviderError(
                "API_SPORTS_KEY is not set. Add it to your .env file."
            )
        self._api_key = api_key

    # ── Public interface ──────────────────────────────────────────────────────

    def search_players(self, search: str) -> tuple[PlayerCandidate, ...]:
        """Search API-Sports for players whose name matches *search*.

        Results are not filtered or ranked — that is the resolver's job.
        Avoid calling this in hot paths; prefer :meth:`get_season_roster` to
        limit results to a known roster (per spec §4 Step 2).
        """
        payload = self._get(self._PLAYERS_ENDPOINT, {"search": search})
        return self._parse_player_response(payload)

    def get_teams(self, external_league_id: str, external_season: str) -> tuple[TeamInfo, ...]:
        """Return all teams competing in a league during a season.

        external_league_id: provider-native league ID (as stored in league_external_ids)
        external_season:    provider-native season ID (as stored in season_external_ids)
        """
        payload = self._get(self._TEAMS_ENDPOINT, {"league": external_league_id, "season": external_season})
        return self._parse_teams_response(payload)

    def get_season_roster(self, external_team_id: str, external_season: str) -> tuple[PlayerCandidate, ...]:
        """Return all players on a team's roster for the given season."""
        payload = self._get(self._PLAYERS_ENDPOINT, {"team": external_team_id, "season": external_season})
        return self._parse_player_response(payload)

    # ── HTTP transport ────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Perform an authenticated GET and return the parsed JSON envelope."""
        path = f"{endpoint}?{urlencode(params)}"
        conn = http.client.HTTPSConnection(self._HOST, timeout=self._REQUEST_TIMEOUT)
        try:
            conn.request("GET", path, headers={"x-apisports-key": self._api_key})
            response = conn.getresponse()
            body = response.read()
        except OSError as exc:
            raise ProviderError(f"Network error contacting API-Sports: {exc}") from exc
        finally:
            conn.close()

        if response.status != 200:
            raise ProviderError(
                f"API-Sports returned HTTP {response.status} for {endpoint}"
            )

        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"API-Sports returned a non-JSON body: {exc}") from exc

        self._validate_envelope(payload, endpoint)
        return payload

    def _validate_envelope(self, payload: dict[str, Any], endpoint: str) -> None:
        """Raise ProviderError if the API-Sports response envelope signals an error."""
        errors = payload.get("errors")
        # API-Sports returns {} or [] when there are no errors; a populated dict signals failure.
        if errors and isinstance(errors, (dict, list)):
            raise ProviderError(f"API-Sports reported errors for {endpoint}: {errors}")

        if "response" not in payload:
            raise ProviderError(
                f"API-Sports response for {endpoint} is missing the 'response' key"
            )

    # ── Player parsing ────────────────────────────────────────────────────────

    def _parse_player_response(self, payload: dict[str, Any]) -> tuple[PlayerCandidate, ...]:
        """Convert the API-Sports player list into a tuple of PlayerCandidate."""
        entries = payload.get("response", [])
        if not isinstance(entries, list):
            raise ProviderError("API-Sports players 'response' is not a list")
        return tuple(self._player_to_candidate(entry) for entry in entries)

    def _player_to_candidate(self, entry: dict[str, Any]) -> PlayerCandidate:
        """Convert one API-Sports player object to a PlayerCandidate."""
        player_id = entry.get("id")
        if player_id is None:
            raise ProviderError(
                f"API-Sports player entry is missing 'id': {entry!r}"
            )

        first_name: str | None = entry.get("firstname") or None
        last_name: str | None = entry.get("lastname") or None

        birth: dict[str, Any] = entry.get("birth") or {}
        birth_date: str | None = birth.get("date") or None
        leagues = entry.get("leagues")

        return PlayerCandidate(
            provider=self.provider_name,
            external_player_id=str(player_id),
            canonical_name=self._build_canonical_name(first_name, last_name),
            first_name=first_name,
            last_name=last_name,
            birth_date=birth_date,
            nationality=entry.get("nationality") or None,
            height_cm=self._parse_height(entry.get("height")),
            position=self._extract_position(leagues),
            jersey_number=self._extract_jersey(leagues),
            # team_id and season_id are ARC-internal keys; the resolver must
            # supply them after confirming which ARC entities correspond to the
            # API-Sports context.  TODO (IdentityResolver): populate these fields.
        )

    # ── Team parsing ──────────────────────────────────────────────────────────

    def _parse_teams_response(self, payload: dict[str, Any]) -> tuple[TeamInfo, ...]:
        """Convert the API-Sports teams list into a tuple of TeamInfo."""
        entries = payload.get("response", [])
        if not isinstance(entries, list):
            raise ProviderError("API-Sports teams 'response' is not a list")
        return tuple(self._team_to_info(entry) for entry in entries)

    def _team_to_info(self, entry: dict[str, Any]) -> TeamInfo:
        """Convert one API-Sports team object to a TeamInfo."""
        team_id = entry.get("id")
        if team_id is None:
            raise ProviderError(
                f"API-Sports team entry is missing 'id': {entry!r}"
            )
        return TeamInfo(
            team_id=int(team_id),
            name=entry.get("name") or "",
            code=entry.get("code") or None,
            city=entry.get("city") or None,
        )

    # ── Field extraction helpers ──────────────────────────────────────────────

    def _build_canonical_name(
        self, first_name: str | None, last_name: str | None
    ) -> str | None:
        """Return '{first} {last}', or None if both parts are absent."""
        parts = [p for p in (first_name, last_name) if p]
        return " ".join(parts) if parts else None

    def _parse_height(self, height_obj: Any) -> int | None:
        """Convert the API-Sports height object to integer centimeters."""
        if not isinstance(height_obj, dict):
            return None
        meters = height_obj.get("meters")
        if meters is None:
            return None
        try:
            return round(float(meters) * 100)
        except (ValueError, TypeError):
            return None

    def _extract_position(self, leagues: Any) -> str | None:
        """Return the normalized position only when all league entries agree; else None.

        TODO (IdentityResolver): When entries disagree, the resolver must select
        the authoritative league for the current resolution context.
        """
        if not isinstance(leagues, dict) or not leagues:
            return None
        positions = {
            self._normalize_position(league["pos"])
            for league in leagues.values()
            if isinstance(league, dict) and league.get("pos")
        }
        positions.discard(None)
        return next(iter(positions)) if len(positions) == 1 else None

    def _extract_jersey(self, leagues: Any) -> str | None:
        """Return the jersey number only when all league entries agree; else None.

        TODO (IdentityResolver): When entries disagree, the resolver must select
        the authoritative league for the current resolution context.
        """
        if not isinstance(leagues, dict) or not leagues:
            return None
        jerseys = {
            str(league["jersey"])
            for league in leagues.values()
            if isinstance(league, dict) and league.get("jersey") is not None
        }
        return next(iter(jerseys)) if len(jerseys) == 1 else None

    # ── Position normalization ────────────────────────────────────────────────

    def _normalize_position(self, raw: str) -> str | None:
        """Map a raw API-Sports position string to an ARC position token.

        Single positions:  ``"PG"`` → ``"G"``, ``"SF"`` → ``"F"``, ``"C"`` → ``"C"``
        Combined positions: ``"PG-SF"`` → ``"G-F"``, ``"PF/C"`` → ``"F-C"``
        Unknown positions:  ``None``
        """
        if not raw:
            return None
        key = raw.strip().lower()

        direct = self._POSITION_MAP.get(key)
        if direct is not None:
            return direct

        # Handle slash/hyphen-delimited multi-position strings (e.g. "PG-SF", "SF/PF").
        # Replace hyphens with slashes first to unify separators, then split.
        tokens = [t.strip() for t in key.replace("-", "/").split("/") if t.strip()]
        if len(tokens) > 1:
            groups: set[str] = {
                self._POSITION_MAP[t]
                for t in tokens
                if t in self._POSITION_MAP and self._POSITION_MAP[t] in self._POSITION_ORDER
            }
            if groups:
                return self._combine_position_groups(groups)

        return None

    def _combine_position_groups(self, groups: set[str]) -> str | None:
        """Combine base ARC group tokens into a valid ARC position string.

        Valid outputs: ``"G"``, ``"F"``, ``"C"``, ``"G-F"``, ``"F-C"``.
        Any other combination returns ``None``.
        """
        ordered = sorted(
            groups & {"G", "F", "C"},
            key=lambda g: self._POSITION_ORDER.index(g),
        )
        candidate = "-".join(ordered)
        return candidate if candidate in {"G", "F", "C", "G-F", "F-C"} else None
