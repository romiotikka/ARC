from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from ..exceptions import ProviderError
from ..models import PlayerCandidate
from .base import IdentityProvider

_POSITION_MAP = {"PG": "G", "SG": "G", "SF": "F", "PF": "F", "C": "C"}


class BasketProvider(IdentityProvider):
    """HTML adapter for the public EstLatBL pages hosted by Basket.ee."""

    provider_name = "basket"
    _DEFAULT_BASE_URL = "https://www.estlatbl.com"
    _PROFILE_PATH = "/et/mangijad?pid={player_id}"
    _PLAYER_LINK = re.compile(r"/et/mangijad\?pid=([^&#\"']+)")

    def __init__(
        self,
        base_url: str | None = None,
        fetch_text: Callable[[str], str] | None = None,
    ) -> None:
        self._base_url = (base_url or self._DEFAULT_BASE_URL).rstrip("/")
        self._fetch_text = fetch_text or self._request_text

    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        """Fetch and normalize a provider team roster from its HTML page."""
        team_id = _required_text(external_team_id, "external team ID")
        season = _required_text(external_season, "external season")
        url = (
            f"{self._base_url}/et/osalevad-voistkonnad/"
            f"{quote_plus(team_id)}//roster?setSid={quote_plus(season)}"
        )
        roster_html = self._fetch(url)
        roster_details = _parse_roster_details(roster_html)
        player_ids = _extract_player_ids(roster_html)
        candidates = []
        for player_id in player_ids:
            candidate = self._fetch_profile(player_id)
            details = roster_details.get(player_id)
            if details is not None:
                candidate = replace(
                    candidate,
                    jersey_number=details.jersey_number or candidate.jersey_number,
                    position=details.position or candidate.position,
                )
            candidates.append(candidate)
        return tuple(candidates)

    def search_players(self, search: str) -> tuple[PlayerCandidate, ...]:
        """Search the public player directory and normalize matching profiles."""
        query = _required_text(search, "player search")
        url = f"{self._base_url}/et/mangijad?keyword={quote_plus(query)}"
        directory_html = self._fetch(url)
        return tuple(self._fetch_profile(player_id) for player_id in _extract_player_ids(directory_html))

    def _fetch_profile(self, player_id: str) -> PlayerCandidate:
        url = self._base_url + self._PROFILE_PATH.format(player_id=quote_plus(player_id))
        profile = _parse_profile(self._fetch(url), player_id)
        if not profile.canonical_name:
            raise ProviderError(f"Basket.ee player profile {player_id!r} has no name")
        return profile

    def _request_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": "ARC identity provider"})
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, OSError) as exc:
            raise ProviderError(f"Basket.ee request failed for {url}: {exc}") from exc

    def _fetch(self, url: str) -> str:
        try:
            return self._fetch_text(url)
        except ProviderError:
            raise
        except (OSError, ValueError) as exc:
            raise ProviderError(f"Basket.ee request failed for {url}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class _ParsedProfile:
    canonical_name: str | None
    first_name: str | None
    last_name: str | None
    birth_date: str | None
    height_cm: int | None
    nationality: str | None
    position: str | None


class _ProfileParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.headings: list[str] = []
        self.text_parts: list[str] = []
        self._heading_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h1", "h2"}:
            self._heading_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "h2"} and self._heading_depth:
            self._heading_depth -= 1

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if not text:
            return
        self.text_parts.append(text)
        if self._heading_depth:
            self.headings.append(text)


@dataclass(frozen=True, slots=True)
class _RosterDetails:
    jersey_number: str | None
    position: str | None


class _RosterParser(HTMLParser):
    _PLAYER_ID = re.compile(r"pid=(\d+)")
    _JERSEY_AND_NAME = re.compile(r"^\s*#?(\d+)\s+(.+?)\s*$")
    _POSITION = re.compile(r"\b(PG|SG|SF|PF|C)\b", re.IGNORECASE)
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.details: dict[str, _RosterDetails] = {}
        self._stack: list[str] = []
        self._player_id: str | None = None
        self._player_depth: int | None = None
        self._capture: str | None = None
        self._capture_depth: int | None = None
        self._capture_text: list[str] = []
        self._position_text: str | None = None
        self._jersey_number: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        depth = len(self._stack) + 1
        classes = set((attributes.get("class") or "").split())
        if tag == "div" and "player" in classes:
            onclick = attributes.get("onclick") or ""
            match = self._PLAYER_ID.search(onclick)
            self._player_id = match.group(1) if match else None
            self._player_depth = depth
            self._position_text = None
            self._jersey_number = None
        elif self._player_id and tag == "div" and "name" in classes:
            self._capture = "name"
            self._capture_depth = depth
            self._capture_text = []
        elif self._player_id and tag == "div" and "positionAndHeight" in classes:
            self._capture = "position"
            self._capture_depth = depth
            self._capture_text = []
        if tag not in self._VOID_TAGS:
            self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        depth = len(self._stack)
        if self._capture and tag == "div" and depth == self._capture_depth:
            value = _clean_text(" ".join(self._capture_text))
            if self._capture == "name":
                match = self._JERSEY_AND_NAME.match(value)
                if match:
                    self._jersey_number = match.group(1)
            else:
                self._position_text = value
            self._capture = None
            self._capture_depth = None
            self._capture_text = []

        if tag == "div" and self._player_id and depth == self._player_depth:
            jersey = getattr(self, "_jersey_number", None)
            position_match = self._POSITION.search(self._position_text or "")
            position = (
                _POSITION_MAP[position_match.group(1).upper()]
                if position_match
                else None
            )
            self.details[self._player_id] = _RosterDetails(jersey, position)
            self._player_id = None
            self._player_depth = None
            self._jersey_number = None

        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._capture_text.append(data)


def _parse_roster_details(html: str) -> dict[str, _RosterDetails]:
    parser = _RosterParser()
    parser.feed(html)
    return parser.details


def _parse_profile(html: str, player_id: str) -> PlayerCandidate:
    parser = _ProfileParser()
    parser.feed(html)
    text = " ".join(parser.text_parts)
    name = next((heading for heading in parser.headings if len(heading.split()) >= 2), None)
    first_name, last_name = _split_name(name)
    position_match = re.search(r"\b(PG|SG|SF|PF|C)\b", text)
    position = (
        {"PG": "G", "SG": "G", "SF": "F", "PF": "F", "C": "C"}[position_match.group(1)]
        if position_match
        else None
    )
    return PlayerCandidate(
        provider=BasketProvider.provider_name,
        external_player_id=str(player_id),
        canonical_name=name,
        first_name=first_name,
        last_name=last_name,
        birth_date=_parse_birth_date(text),
        height_cm=_parse_height(text),
        nationality=_parse_labeled_value(text, "Riik"),
        position=position,
    )


def _extract_player_ids(html: str) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for match in BasketProvider._PLAYER_LINK.finditer(html):
        player_id = match.group(1).strip()
        if player_id and player_id not in seen:
            seen.add(player_id)
            result.append(player_id)
    return tuple(result)


def _parse_height(text: str) -> int | None:
    match = re.search(r"Pikkus\s*:\s*(\d+)\s*cm", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _parse_birth_date(text: str) -> str | None:
    match = re.search(r"Sünniaeg\s*:\s*(\d{2}\.\d{2}\.\d{4})", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _parse_labeled_value(text: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}\s*:\s*(.+?)(?=\s+(?:Statistika|Pikkus|Kaal|Sünniaeg|Riik|Power forward|Small forward|Point guard|Shooting guard|Center)\b|$)",
        text,
        re.IGNORECASE,
    )
    return _clean_text(match.group(1)) if match else None


def _split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
        return first or None, last or None
    parts = name.split()
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) >= 2 else (None, parts[0])


def _clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _required_text(value: str, label: str) -> str:
    cleaned = _clean_text(str(value))
    if not cleaned:
        raise ProviderError(f"Basket.ee {label} is required")
    return cleaned
