"""
tests/test_resolver.py

Standalone test suite for IdentityResolver.
Run from the project root:  python tests/test_resolver.py
"""
from __future__ import annotations

import sqlite3
import sys
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.identity.exceptions import ManualReviewRequired
from scripts.identity.models import IdentityContext, PlayerCandidate, ResolverStatus
from scripts.identity.providers.base import IdentityProvider
from scripts.identity.resolver import (
    IdentityResolver,
    _candidate_lookup_terms,
    _parse_name_parts,
    _score_candidate_name,
    _score_candidate_with_context,
    _score_first_names,
    _score_name,
    _split_display_name,
)

# ── Minimal schema ────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE players (
    player_id      TEXT PRIMARY KEY,
    first_name     TEXT,
    last_name      TEXT,
    canonical_name TEXT NOT NULL,
    birth_date     TEXT,
    nationality    TEXT,
    height_cm      INTEGER,
    position       TEXT,
    identity_status TEXT NOT NULL DEFAULT 'unverified',
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE player_aliases (
    alias_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id  TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    source     TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    UNIQUE (player_id, alias_name)
);
CREATE TABLE player_external_ids (
    player_id          TEXT NOT NULL,
    provider           TEXT NOT NULL,
    external_player_id TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(player_id),
    PRIMARY KEY (player_id, provider, external_player_id),
    UNIQUE (provider, external_player_id)
);
CREATE TABLE team_external_ids (
    team_id          TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    external_team_id TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (team_id, provider),
    UNIQUE (provider, external_team_id)
);
CREATE TABLE season_external_ids (
    season_id          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    external_season_id TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (season_id, provider),
    UNIQUE (provider, external_season_id)
);
CREATE TABLE league_external_ids (
    league_id          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    external_league_id TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (league_id, provider),
    UNIQUE (provider, external_league_id)
);
CREATE TABLE games (
    game_id TEXT PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season_id INTEGER NOT NULL
);
CREATE TABLE player_games (
    game_id TEXT NOT NULL,
    player_id TEXT,
    team_id TEXT,
    shirt_number TEXT,
    position TEXT
);
"""


def _make_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _seed(
    db: sqlite3.Connection,
    player_id: str,
    canonical_name: str,
    first_name: str | None = None,
    last_name: str | None = None,
    aliases: list[str] | None = None,
) -> None:
    db.execute(
        "INSERT INTO players (player_id, canonical_name, first_name, last_name) VALUES (?,?,?,?)",
        (player_id, canonical_name, first_name, last_name),
    )
    for alias in aliases or []:
        db.execute(
            "INSERT INTO player_aliases (player_id, alias_name, source) VALUES (?,?,'test')",
            (player_id, alias),
        )
    db.commit()


def _ctx(
    raw_name: str,
    *,
    jersey: str | None = None,
    team_id: str = "TLM",
    season_id: int = 3,
    league_id: int = 1,
    position: str | None = None,
) -> IdentityContext:
    return IdentityContext(
        raw_name=raw_name,
        team_id=team_id,
        season_id=season_id,
        league_id=league_id,
        jersey_number=jersey,
        position=position,
    )


def _seed_history(
    db: sqlite3.Connection,
    player_id: str,
    *,
    game_id: str,
    team_id: str,
    season_id: int = 3,
    league_id: int = 1,
    jersey: str | None = None,
    position: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO games (game_id, league_id, season_id) VALUES (?,?,?)",
        (game_id, league_id, season_id),
    )
    db.execute(
        """INSERT INTO player_games
           (game_id, player_id, team_id, shirt_number, position)
           VALUES (?,?,?,?,?)""",
        (game_id, player_id, team_id, jersey, position),
    )
    db.commit()


def _aliases(db: sqlite3.Connection, player_id: str) -> list[str]:
    rows = db.execute(
        "SELECT alias_name FROM player_aliases WHERE player_id = ?", (player_id,)
    ).fetchall()
    return [r["alias_name"] for r in rows]


def _player_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM players").fetchone()[0]


def _get(db: sqlite3.Connection, player_id: str) -> dict:
    return dict(db.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone())


def _ext_player_id(db: sqlite3.Connection, provider: str, player_id: str) -> str | None:
    row = db.execute(
        "SELECT external_player_id FROM player_external_ids WHERE provider=? AND player_id=?",
        (provider, player_id),
    ).fetchone()
    return row["external_player_id"] if row else None


def _team_ext_id(db: sqlite3.Connection, provider: str, team_id: str) -> str | None:
    row = db.execute(
        "SELECT external_team_id FROM team_external_ids WHERE provider=? AND team_id=?",
        (provider, team_id),
    ).fetchone()
    return row["external_team_id"] if row else None


def _season_ext_id(db: sqlite3.Connection, provider: str, season_id: int) -> str | None:
    row = db.execute(
        "SELECT external_season_id FROM season_external_ids WHERE provider=? AND season_id=?",
        (provider, season_id),
    ).fetchone()
    return row["external_season_id"] if row else None


def _league_ext_id(db: sqlite3.Connection, provider: str, league_id: int) -> str | None:
    row = db.execute(
        "SELECT external_league_id FROM league_external_ids WHERE provider=? AND league_id=?",
        (provider, league_id),
    ).fetchone()
    return row["external_league_id"] if row else None


# ── Fake provider for testing ─────────────────────────────────────────────────

class FakeProvider(IdentityProvider):
    """Test stub that returns a fixed roster without making HTTP calls."""

    provider_name = "fake"

    def __init__(self, roster: list[PlayerCandidate]) -> None:
        self._roster = tuple(roster)

    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        return self._roster


class NamedFakeProvider(IdentityProvider):
    """Test provider with configurable provider_name for fallback-chain tests."""

    def __init__(self, provider_name: str, roster: list[PlayerCandidate]) -> None:
        self.provider_name = provider_name
        self._roster = tuple(roster)

    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        return self._roster


class TrackingProvider(NamedFakeProvider):
    """Provider fixture that records exactly what the resolver passes to it."""

    def __init__(self, provider_name: str, roster: list[PlayerCandidate]) -> None:
        super().__init__(provider_name, roster)
        self.calls: list[tuple[str, str]] = []

    def get_season_roster(
        self, external_team_id: str, external_season: str
    ) -> tuple[PlayerCandidate, ...]:
        self.calls.append((external_team_id, external_season))
        return super().get_season_roster(external_team_id, external_season)


def _candidate(
    ext_id: str,
    canonical: str,
    first: str | None = None,
    last: str | None = None,
    provider: str = "fake",
    **kwargs,
) -> PlayerCandidate:
    return PlayerCandidate(
        provider=provider,
        external_player_id=ext_id,
        canonical_name=canonical,
        first_name=first,
        last_name=last,
        **kwargs,
    )


# ── Test harness ──────────────────────────────────────────────────────────────

_passed = _failed = 0


def check(label: str, condition: bool) -> None:
    global _passed, _failed
    if condition:
        print(f"  PASS  {label}")
        _passed += 1
    else:
        print(f"  FAIL  {label}")
        _failed += 1


# ── Scenario 1: existing player changes team ──────────────────────────────────

def test_team_change_does_not_duplicate_player() -> None:
    print("\n[1] Existing player changes team — same name form")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    res1 = r.resolve(_ctx("Kristjan Kitsing"))
    check("first resolve: MATCH",         res1.status == ResolverStatus.MATCH)
    check("first resolve: correct id",    res1.player_id == "abc")
    check("first resolve: confidence 1.0", res1.confidence == 1.0)

    # Same player, different team context — still the same person
    res2 = r.resolve(_ctx("Kristjan Kitsing"))
    check("second resolve: same player_id", res2.player_id == "abc")
    check("no duplicate created",           _player_count(db) == 1)


# ── Scenario 2: same player with a different name form ────────────────────────

def test_initial_form_matches_full_name() -> None:
    print("\n[2] Same player — initial name form 'K. Kitsing'")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    res = r.resolve(_ctx("K. Kitsing"))
    check("status MATCH",            res.status == ResolverStatus.MATCH)
    check("same player_id",          res.player_id == "abc")
    check("confidence ~0.80",        res.confidence == 0.80)
    check("no duplicate",            _player_count(db) == 1)
    # 0.80 < _STRONG_MATCH (0.90): alias must NOT be auto-learned
    check("no alias learned",        "K. Kitsing" not in _aliases(db, "abc"))


def test_comma_format_matches_and_learns_alias() -> None:
    print("\n[2b] Same player — 'Last, First' format (score 0.90 → alias learned)")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    res = r.resolve(_ctx("Kitsing, Kristjan"))
    check("status MATCH",            res.status == ResolverStatus.MATCH)
    check("same player_id",          res.player_id == "abc")
    check("confidence 0.90",         res.confidence == 0.90)
    check("alias learned",           "Kitsing, Kristjan" in _aliases(db, "abc"))


def test_stored_alias_resolves_directly() -> None:
    print("\n[2c] Same player — incoming name already stored as alias (score 0.95)")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing", aliases=["K. Kitsing"])
    r = IdentityResolver(db)

    res = r.resolve(_ctx("K. Kitsing"))
    check("status MATCH",            res.status == ResolverStatus.MATCH)
    check("same player_id",          res.player_id == "abc")
    check("confidence 0.95",         res.confidence == 0.95)


def test_confirmed_alias_is_idempotent() -> None:
    print("\n[2d] Confirmed aliases are learned once and never duplicated")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    resolver = IdentityResolver(db)

    resolver.resolve(_ctx("Kitsing, Kristjan"))
    resolver.resolve(_ctx("Kitsing, Kristjan"))
    count = db.execute(
        "SELECT COUNT(*) FROM player_aliases WHERE player_id = ? AND alias_name = ?",
        ("abc", "Kitsing, Kristjan"),
    ).fetchone()[0]

    check("one alias row after repeated confirmed occurrence", count == 1)


# ── Scenario 3: genuinely new player ─────────────────────────────────────────

def test_new_player_is_created() -> None:
    print("\n[3] Genuinely new player — no plausible match")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    res = r.resolve(_ctx("Marcus Johnson"))
    check("status CREATED",                     res.status == ResolverStatus.CREATED)
    check("player_id assigned",                 res.player_id is not None)
    check("player_id differs from existing",    res.player_id != "abc")
    check("two players in DB",                  _player_count(db) == 2)

    new = _get(db, res.player_id)
    check("canonical_name preserved",   new["canonical_name"] == "Marcus Johnson")
    check("last_name left unknown",      new["last_name"] is None)
    check("first_name left unknown",     new["first_name"] is None)
    check("identity_status unverified",  new["identity_status"] == "unverified")


def test_empty_db_creates_player() -> None:
    print("\n[3b] Empty DB — first player ever")
    db = _make_db()
    r = IdentityResolver(db)

    res = r.resolve(_ctx("Andris Biedriņš"))
    check("status CREATED",      res.status == ResolverStatus.CREATED)
    check("one player in DB",    _player_count(db) == 1)
    p = _get(db, res.player_id)
    check("canonical_name ok",   p["canonical_name"] == "Andris Biedriņš")
    check("structured fields unknown", p["first_name"] is None and p["last_name"] is None)


# ── Scenario 4: ambiguous name collision ─────────────────────────────────────

def test_comma_name_creation_keeps_safe_structure() -> None:
    print("\n[3c] Explicit comma format supplies safe structured fields")
    db = _make_db()
    result = IdentityResolver(db).resolve(_ctx("Johnson, Marcus"))
    player = _get(db, result.player_id)

    check("created", result.status == ResolverStatus.CREATED)
    check("canonical label preserved", player["canonical_name"] == "Johnson, Marcus")
    check("first name safely populated", player["first_name"] == "Marcus")
    check("last name safely populated", player["last_name"] == "Johnson")


def test_ambiguous_collision_requires_review() -> None:
    print("\n[4] Ambiguous — 'J. Smith' with two Smith players")
    db = _make_db()
    _seed(db, "x1", "John Smith",  "John",  "Smith")
    _seed(db, "x2", "James Smith", "James", "Smith")
    r = IdentityResolver(db)

    raised = False
    try:
        r.resolve(_ctx("J. Smith"))
    except ManualReviewRequired:
        raised = True

    check("ManualReviewRequired raised", raised)
    check("no new player created",       _player_count(db) == 2)


def test_single_candidate_low_score_creates() -> None:
    print("\n[4b] Single candidate, last-name-only input — score in review range")
    # 'Kitsing' (bare last name) against 'Kristjan Kitsing' scores 0.55:
    # last name matches but incoming has no first name → _score_first_names(None, 'kristjan') = 0.55
    # 0.55 >= _REVIEW_FLOOR (0.50) → ManualReviewRequired, not a new player
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    raised = False
    try:
        r.resolve(_ctx("Kitsing"))
    except ManualReviewRequired:
        raised = True

    check("bare last-name triggers review (score 0.55 ≥ floor)", raised)
    check("no new player created", _player_count(db) == 1)


def test_created_result_confidence_is_not_overstated() -> None:
    print("\n[4c] Created identity confidence is not reported as certain")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    resolver = IdentityResolver(db)

    result = resolver.resolve(_ctx("Marcus Johnson"))
    check("status CREATED", result.status == ResolverStatus.CREATED)
    check("creation confidence is neutral", result.confidence == 0.0)


# ── Scenario 5: external ID round-trip ───────────────────────────────────────

def test_external_id_mapping() -> None:
    print("\n[5] External ID: store and retrieve")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    r.store_external_id("abc", "api_sports", "99999")
    found   = r.resolve_by_external_id("api_sports", "99999")
    missing = r.resolve_by_external_id("api_sports", "00000")

    check("known external ID returns player_id",   found == "abc")
    check("unknown external ID returns None",       missing is None)

    # Idempotency: storing the same mapping twice must not raise
    r.store_external_id("abc", "api_sports", "99999")
    check("duplicate store is idempotent", True)


# ── Scenario 6: metadata enrichment ─────────────────────────────────────────

def test_update_metadata_fills_nulls_only() -> None:
    print("\n[6] update_player_metadata — fills NULL fields, never overwrites")
    from scripts.identity.models import PlayerCandidate

    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    # Manually set birth_date to an existing value
    db.execute("UPDATE players SET birth_date = '1988-03-15' WHERE player_id = 'abc'")
    db.commit()

    r = IdentityResolver(db)
    candidate = PlayerCandidate(
        provider="api_sports",
        external_player_id="99999",
        canonical_name="Kristjan Kitsing",
        first_name="Kristjan",
        last_name="Kitsing",
        birth_date="1990-01-01",   # should NOT overwrite existing
        nationality="Estonian",     # should be stored (was NULL)
        height_cm=204,              # should be stored (was NULL)
        position="F",               # should be stored (was NULL)
    )
    r.update_player_metadata("abc", candidate)

    p = _get(db, "abc")
    check("birth_date NOT overwritten",  p["birth_date"]  == "1988-03-15")
    check("nationality filled",          p["nationality"] == "Estonian")
    check("height_cm filled",            p["height_cm"]   == 204)
    check("position filled",             p["position"]    == "F")


# ── Unit tests: scoring functions ─────────────────────────────────────────────

def test_score_name_unit() -> None:
    print("\n[7] _score_name() unit tests")

    player = {
        "canonical_name": "Kristjan Kitsing",
        "first_name":     "Kristjan",
        "last_name":      "Kitsing",
        "aliases":        [],
    }

    check("exact canonical → 1.00",        _score_name("Kristjan Kitsing",  player) == 1.00)
    check("uppercase variant → 1.00",       _score_name("KRISTJAN KITSING",  player) == 1.00)
    check("initial form → 0.80",            _score_name("K. Kitsing",        player) == 0.80)
    check("initial no dot → 0.80",          _score_name("K Kitsing",         player) == 0.80)
    check("comma format → 0.90",            _score_name("Kitsing, Kristjan", player) == 0.90)
    check("conflicting first → 0.50",       _score_name("Andrei Kitsing",    player) == 0.50)
    check("completely different → 0.00",    _score_name("Marcus Johnson",    player) == 0.00)

    player_no_first = {
        "canonical_name": "K. Kitsing",
        "first_name":     None,
        "last_name":      None,   # NULL forces fallback to parsing canonical name
        "aliases":        [],
    }
    # last_name NULL → fallback parses 'K. Kitsing' → ex_first='k', ex_last='kitsing'
    # _score_first_names(None, 'k') = 0.55 (incoming has no first name, existing has initial)
    check("last name only, ex has initial → 0.55", _score_name("Kitsing", player_no_first) == 0.55)

    player_with_alias = {
        "canonical_name": "Kristjan Kitsing",
        "first_name":     "Kristjan",
        "last_name":      "Kitsing",
        "aliases":        ["K. Kitsing"],
    }
    check("matches stored alias → 0.95",    _score_name("K. Kitsing", player_with_alias) == 0.95)


def test_score_candidate_name_unit() -> None:
    print("\n[7b] _score_candidate_name() unit tests")
    c = _candidate("99", "Kristjan Kitsing", "Kristjan", "Kitsing")
    check("exact match → 1.00",   _score_candidate_name("Kristjan Kitsing", c) == 1.00)
    check("initial form → 0.80",  _score_candidate_name("K. Kitsing",       c) == 0.80)
    check("different → 0.00",     _score_candidate_name("Marcus Johnson",   c) == 0.00)


def test_parse_name_parts_unit() -> None:
    print("\n[8] _parse_name_parts() unit tests")
    check("'First Last'",          _parse_name_parts("Kristjan Kitsing")   == ("kristjan", "kitsing"))
    check("'Last, First'",         _parse_name_parts("Kitsing, Kristjan")  == ("kristjan", "kitsing"))
    check("'F. Last'",             _parse_name_parts("K. Kitsing")         == ("k", "kitsing"))
    check("bare last name",        _parse_name_parts("Kitsing")            == (None, "kitsing"))
    check("empty string",          _parse_name_parts("")                   == (None, None))


def test_split_display_name_unit() -> None:
    print("\n[9] _split_display_name() unit tests")
    check("'First Last'",   _split_display_name("Kristjan Kitsing")  == ("Kristjan", "Kitsing"))
    check("'Last, First'",  _split_display_name("Kitsing, Kristjan") == ("Kristjan", "Kitsing"))
    check("'F. Last'",      _split_display_name("K. Kitsing")        == ("K.", "Kitsing"))
    check("bare last name", _split_display_name("Kitsing")           == (None, "Kitsing"))


# ── Provider roster tests ─────────────────────────────────────────────────────

def _setup_provider_db(roster: list[PlayerCandidate]) -> tuple[sqlite3.Connection, IdentityResolver]:
    """Return an in-memory DB + resolver with team/season mappings and a FakeProvider."""
    db = _make_db()
    provider = FakeProvider(roster)
    r = IdentityResolver(db, providers=[provider])
    r.store_team_external_id("TLM", "fake", "ext-team-1")
    r.store_season_external_id(3, "fake", "2025")
    r.store_league_external_id(1, "fake", "league-2025")
    return db, r


def test_provider_direct_external_id_match() -> None:
    print("\n[10] Provider — known external_player_id resolves directly (no name scoring)")
    roster = [_candidate("pid-99", "Kristjan Kitsing", "Kristjan", "Kitsing")]
    db, r = _setup_provider_db(roster)
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r.store_external_id("abc", "fake", "pid-99")

    res = r.resolve(_ctx("K. Kitsing"))  # name form differs — ext ID should bypass scoring
    check("status MATCH",          res.status == ResolverStatus.MATCH)
    check("returns existing player", res.player_id == "abc")
    check("confidence 1.0",        res.confidence == 1.0)
    check("no duplicate",          _player_count(db) == 1)
    # Incoming name alias learned because external ID confirmed identity
    check("alias learned via ext ID", "K. Kitsing" in _aliases(db, "abc"))


def test_provider_team_change_same_external_id() -> None:
    print("\n[11] Provider — player moves to new team; same external ID reuses same player")
    roster = [_candidate("pid-99", "Kristjan Kitsing", "Kristjan", "Kitsing")]
    db, r = _setup_provider_db(roster)
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r.store_external_id("abc", "fake", "pid-99")

    # Resolve with a DIFFERENT team — the external ID still points to the same ARC player
    r.store_team_external_id("BIG5", "fake", "ext-team-2")
    res = r.resolve(_ctx("Kristjan Kitsing", team_id="BIG5"))
    check("same player_id despite team change", res.player_id == "abc")
    check("no duplicate player",                _player_count(db) == 1)


def test_provider_roster_creates_new_player() -> None:
    print("\n[12] Provider — new external_player_id creates a new ARC player")
    roster = [_candidate("pid-42", "Marcus Johnson", "Marcus", "Johnson",
                         birth_date="1995-06-15", nationality="USA", height_cm=198)]
    db, r = _setup_provider_db(roster)

    res = r.resolve(_ctx("Marcus Johnson"))
    check("status CREATED",              res.status == ResolverStatus.CREATED)
    check("player_id assigned",          res.player_id is not None)
    check("one player in DB",            _player_count(db) == 1)

    p = _get(db, res.player_id)
    check("canonical from provider",     p["canonical_name"] == "Marcus Johnson")
    check("first_name from provider",    p["first_name"] == "Marcus")
    check("last_name from provider",     p["last_name"] == "Johnson")
    check("birth_date from provider",    p["birth_date"] == "1995-06-15")
    check("nationality from provider",   p["nationality"] == "USA")
    check("height_cm from provider",     p["height_cm"] == 198)
    # External ID must be stored as a TEXT string
    stored = _ext_player_id(db, "fake", res.player_id)
    check("external ID stored as str",   stored == "pid-42")
    check("external ID is str type",     isinstance(stored, str))


def test_provider_roster_links_to_existing_player() -> None:
    print("\n[13] Provider — roster name-match links external ID to existing ARC player")
    roster = [_candidate("pid-77", "Kristjan Kitsing", "Kristjan", "Kitsing")]
    db, r = _setup_provider_db(roster)
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")

    res = r.resolve(_ctx("K. Kitsing"))
    check("status MATCH",               res.status == ResolverStatus.MATCH)
    check("existing player reused",     res.player_id == "abc")
    check("no duplicate",               _player_count(db) == 1)
    # External ID must now be stored linking the provider to the existing player
    check("external ID linked to abc",  _ext_player_id(db, "fake", "abc") == "pid-77")
    # Alias learned because external ID confirmed identity
    check("alias K. Kitsing learned",   "K. Kitsing" in _aliases(db, "abc"))


def test_provider_roster_ambiguous_requires_review() -> None:
    print("\n[14] Provider — two similar roster entries trigger ManualReviewRequired")
    roster = [
        _candidate("pid-1", "John Smith",  "John",  "Smith"),
        _candidate("pid-2", "James Smith", "James", "Smith"),
    ]
    db, r = _setup_provider_db(roster)

    raised = False
    try:
        r.resolve(_ctx("J. Smith"))
    except ManualReviewRequired:
        raised = True

    check("ManualReviewRequired raised", raised)
    check("no player created",           _player_count(db) == 0)


def test_team_external_id_stored_and_retrieved() -> None:
    print("\n[15] Team external ID — stored as TEXT and retrieved correctly")
    db = _make_db()
    r = IdentityResolver(db)
    r.store_team_external_id("TLM", "api_sports", "12345")
    r.store_team_external_id("BIG5", "api_sports", "67890")

    check("TLM maps to 12345",      _team_ext_id(db, "api_sports", "TLM")  == "12345")
    check("BIG5 maps to 67890",     _team_ext_id(db, "api_sports", "BIG5") == "67890")
    check("stored value is str",    isinstance(_team_ext_id(db, "api_sports", "TLM"), str))
    # Idempotent
    r.store_team_external_id("TLM", "api_sports", "12345")
    check("duplicate is idempotent", True)


def test_season_external_id_stored_and_retrieved() -> None:
    print("\n[16] Season external ID — stored as TEXT even for numeric values")
    db = _make_db()
    r = IdentityResolver(db)
    r.store_season_external_id(3, "api_sports", "2025")
    r.store_season_external_id(4, "api_sports", "2026")

    check("season 3 → '2025'",      _season_ext_id(db, "api_sports", 3) == "2025")
    check("season 4 → '2026'",      _season_ext_id(db, "api_sports", 4) == "2026")
    check("stored value is str",    isinstance(_season_ext_id(db, "api_sports", 3), str))
    # Idempotent
    r.store_season_external_id(3, "api_sports", "2025")
    check("duplicate is idempotent", True)


def test_league_external_id_stored_and_retrieved() -> None:
    print("\n[16b] League external ID - provider-specific TEXT mapping")
    db = _make_db()
    r = IdentityResolver(db)
    r.store_league_external_id(1, "api_sports", "987")
    r.store_league_external_id(1, "basket", "estlatbl-2025")

    check("API-Sports league mapping", _league_ext_id(db, "api_sports", 1) == "987")
    check("Basket league mapping", _league_ext_id(db, "basket", 1) == "estlatbl-2025")
    check("external league ID is str", isinstance(_league_ext_id(db, "api_sports", 1), str))


def test_provider_calls_require_all_entity_mappings() -> None:
    print("\n[16c] Provider coverage requires team, season, and league mappings")
    db = _make_db()
    provider_a = TrackingProvider("p_a", [_candidate("a-1", "Other Name", provider="p_a")])
    provider_b = TrackingProvider(
        "p_b", [_candidate("b-1", "Kristjan Kitsing", "Kristjan", "Kitsing", provider="p_b")]
    )
    r = IdentityResolver(db, providers=[provider_a, provider_b])
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")

    # Provider A has team/season mappings but does not cover this ARC league.
    r.store_team_external_id("TLM", "p_a", "external-team-a")
    r.store_season_external_id(3, "p_a", "season-a")

    # Provider B covers the same ARC entities with its own external strings.
    r.store_team_external_id("TLM", "p_b", "external-team-b")
    r.store_season_external_id(3, "p_b", "season-b")
    r.store_league_external_id(1, "p_b", "league-b")

    result = r.resolve(_ctx("K. Kitsing"))
    check("same ARC team maps differently per provider", _team_ext_id(db, "p_a", "TLM") == "external-team-a" and _team_ext_id(db, "p_b", "TLM") == "external-team-b")
    check("same ARC season maps differently per provider", _season_ext_id(db, "p_a", 3) == "season-a" and _season_ext_id(db, "p_b", 3) == "season-b")
    check("provider without league mapping skipped", provider_a.calls == [])
    check("mapped provider receives external IDs only", provider_b.calls == [("external-team-b", "season-b")])
    check("second provider resolves", result.player_id == "abc")


def test_no_provider_mapping_falls_back_to_name_search() -> None:
    print("\n[17] No provider mapping — falls through to name/alias matching")
    roster = [_candidate("pid-99", "Kristjan Kitsing", "Kristjan", "Kitsing")]
    db = _make_db()
    provider = FakeProvider(roster)
    r = IdentityResolver(db, providers=[provider])
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    # No team or season mapping stored → provider is bypassed

    res = r.resolve(_ctx("Kristjan Kitsing"))
    check("falls back to name MATCH", res.status == ResolverStatus.MATCH)
    check("correct player_id",        res.player_id == "abc")
    check("no new player created",    _player_count(db) == 1)


def test_conflicting_arc_player_blocks_creation() -> None:
    print("\n[18] Conflicting ARC player blocks duplicate creation")
    # ARC has 'Kris Kitsing'. Provider has 'Kristjan Kitsing' (new ext_id).
    # Score of 'Kristjan Kitsing' vs 'Kris Kitsing' = 0.50 (conflicting first names)
    # → ManualReviewRequired, NOT a new player.
    roster = [_candidate("pid-new", "Kristjan Kitsing", "Kristjan", "Kitsing")]
    db, r = _setup_provider_db(roster)
    _seed(db, "kris", "Kris Kitsing", "Kris", "Kitsing")

    raised = False
    try:
        r.resolve(_ctx("K. Kitsing"))
    except ManualReviewRequired:
        raised = True

    check("ManualReviewRequired raised",     raised)
    check("no duplicate player created",     _player_count(db) == 1)
    check("conflicting player unchanged",    _get(db, "kris")["canonical_name"] == "Kris Kitsing")


def test_cross_provider_alias_resolution() -> None:
    print("\n[19] Cross-provider: second provider links its ext_id to existing ARC player")
    # ARC already has 'abc' = 'Kristjan Kitsing', linked to 'other_provider'/'op-123'.
    # FakeProvider (second provider) also has 'Kristjan Kitsing' with ext_id 'fp-88'.
    # Resolving via FakeProvider should MATCH 'abc' and link 'fp-88' to it.
    roster = [_candidate("fp-88", "Kristjan Kitsing", "Kristjan", "Kitsing")]
    db, r = _setup_provider_db(roster)
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    # Simulate an existing link from a different provider
    db.execute(
        "INSERT INTO player_external_ids (player_id, provider, external_player_id) "
        "VALUES ('abc', 'other_provider', 'op-123')"
    )
    db.commit()

    res = r.resolve(_ctx("Kristjan Kitsing"))
    check("status MATCH",                      res.status == ResolverStatus.MATCH)
    check("returns existing player",           res.player_id == "abc")
    check("no duplicate created",             _player_count(db) == 1)
    check("fake ext_id now linked to abc",    _ext_player_id(db, "fake", "abc") == "fp-88")
    check("original other_provider link kept", r.resolve_by_external_id("other_provider", "op-123") == "abc")


def test_last_first_without_comma_matches() -> None:
    print("\n[20] Name form — 'Last First' without comma matches existing player")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    res = r.resolve(_ctx("Kitsing Kristjan"))
    check("status MATCH",          res.status == ResolverStatus.MATCH)
    check("same player_id",        res.player_id == "abc")
    check("no duplicate",          _player_count(db) == 1)


def test_middle_name_variant_matches_existing() -> None:
    print("\n[21] Name form — middle-name variant matches existing player")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r = IdentityResolver(db)

    res = r.resolve(_ctx("Kristjan Mait Kitsing"))
    check("status MATCH",          res.status == ResolverStatus.MATCH)
    check("same player_id",        res.player_id == "abc")
    check("no duplicate",          _player_count(db) == 1)


def test_flexible_name_forms_and_normalization() -> None:
    print("\n[21b] Flexible forms - reversed middle names, punctuation, and diacritics")
    db = _make_db()
    _seed(db, "kitsing", "Kristjan Kitsing", "Kristjan", "Kitsing")
    _seed(db, "smits", "Artūrs Šmits", "Artūrs", "Šmits", aliases=["A. Šmits"])
    r = IdentityResolver(db)

    forms = ["Kristjan M. Kitsing", "Kristjan Mait Kitsing", "Kitsing Kristjan Mait"]
    check("all Kitsing forms resolve", all(r.resolve(_ctx(name)).player_id == "kitsing" for name in forms))
    check("diacritic/punctuation alias resolves", r.resolve(_ctx("A Smits")).player_id == "smits")
    check("strong middle-name form learned as alias", "Kristjan M. Kitsing" in _aliases(db, "kitsing"))


def test_candidate_lookup_terms_do_not_use_interior_fragments() -> None:
    print("\n[21ba] Candidate lookup terms never use arbitrary interior fragments")
    terms = set(_candidate_lookup_terms("A. Šmits"))
    check("raw token included", "Šmits" in terms)
    check("normalized token included", "smits" in terms)
    check("interior fragment excluded", "mit" not in terms and "mits" not in terms)


def test_diacritic_canonical_discovery_without_existing_ascii_alias() -> None:
    print("\n[21baa] Diacritic canonical name is discoverable from ASCII input")
    db = _make_db()
    _seed(db, "smits", "Artūrs Šmits", "Artūrs", "Šmits")
    resolver = IdentityResolver(db)

    candidates = resolver._search_candidates("A Smits")
    check("candidate discovered without pre-learned ASCII alias", [candidate["player_id"] for candidate in candidates] == ["smits"])
    result = resolver.resolve(_ctx("A Smits"))
    check("identity resolves to existing player", result.player_id == "smits")


def test_local_discovery_is_targeted_across_name_forms() -> None:
    print("\n[21bb] Local candidate discovery supports ordering, initials, and aliases")
    db = _make_db()
    _seed(db, "kitsing", "Kristjan Mait Kitsing", "Kristjan Mait", "Kitsing")
    _seed(db, "smits", "Arturs Smits", "Arturs", "Smits", aliases=["A. Šmits"])
    _seed(db, "unrelated", "Alex Morgan", "Alex", "Morgan")
    resolver = IdentityResolver(db)

    kitsing_forms = [
        "Kristjan Kitsing", "K. Kitsing", "Kitsing, Kristjan",
        "Kitsing Kristjan", "Kristjan M. Kitsing", "Kitsing Kristjan Mait",
    ]
    check(
        "all Kitsing forms discover only Kitsing",
        all(
            [candidate["player_id"] for candidate in resolver._search_candidates(name)] == ["kitsing"]
            for name in kitsing_forms
        ),
    )
    check(
        "diacritic alias discovers Smits",
        [candidate["player_id"] for candidate in resolver._search_candidates("A Smits")] == ["smits"],
    )
    check(
        "unrelated player is not scanned into result",
        "unrelated" not in [candidate["player_id"] for candidate in resolver._search_candidates("K. Kitsing")],
    )


def test_local_history_disambiguates_only_plausible_initials() -> None:
    print("\n[21bc] PlayerGame history separates plausible initial collisions")
    db = _make_db()
    _seed(db, "kristjan", "Kristjan Kitsing", "Kristjan", "Kitsing")
    _seed(db, "kaur", "Kaur Kitsing", "Kaur", "Kitsing")
    _seed_history(
        db, "kristjan", game_id="g-kristjan", team_id="TLM",
        jersey="7", position="F",
    )
    _seed_history(
        db, "kaur", game_id="g-kaur", team_id="OTHER",
        jersey="11", position="G",
    )
    resolver = IdentityResolver(db)

    result = resolver.resolve(_ctx("K. Kitsing", jersey="7", position="F"))
    check("history resolves the matching player", result.player_id == "kristjan")
    check("no duplicate created", _player_count(db) == 2)

    raised = False
    try:
        resolver.resolve(_ctx("Kris Kitsing", jersey="7", position="F"))
    except ManualReviewRequired:
        raised = True
    check("history cannot rescue a conflicting first name", raised)


def test_local_history_is_neutral_for_normal_team_moves() -> None:
    print("\n[21bd] Missing scoped history does not block team, season, or league moves")
    db = _make_db()
    _seed(db, "kitsing", "Kristjan Kitsing", "Kristjan", "Kitsing")
    _seed_history(db, "kitsing", game_id="old-game", team_id="OLD", season_id=2, league_id=2)
    resolver = IdentityResolver(db)

    moves = [
        _ctx("Kristjan Kitsing", team_id="NEW", season_id=2, league_id=2),
        _ctx("Kristjan Kitsing", team_id="NEW", season_id=3, league_id=2),
        _ctx("Kristjan Kitsing", team_id="NEW", season_id=3, league_id=1),
    ]
    check("all ordinary moves retain the identity", all(resolver.resolve(move).player_id == "kitsing" for move in moves))


def test_conflicting_explicit_middle_names_require_review() -> None:
    print("\n[21c] Conflicting explicit middle names do not auto-merge")
    db = _make_db()
    _seed(db, "john", "John Robert Smith", "John Robert", "Smith")
    r = IdentityResolver(db)

    raised = False
    try:
        r.resolve(_ctx("John James Smith"))
    except ManualReviewRequired:
        raised = True
    check("conflicting middle name requires review", raised)
    check("no duplicate created", _player_count(db) == 1)


def test_known_source_id_with_conflicting_name_requires_review() -> None:
    print("\n[21d] Known source ID does not override conflicting name evidence")
    db = _make_db()
    _seed(db, "john", "John Smith", "John", "Smith")
    r = IdentityResolver(db)
    r.store_external_id("john", "fiba_livestats", "live-7")

    raised = False
    try:
        r.resolve(IdentityContext(
            raw_name="James Brown", team_id="TLM", season_id=3, league_id=1,
            provider="fiba_livestats", external_player_id="live-7",
        ))
    except ManualReviewRequired:
        raised = True
    check("conflicting source ID requires review", raised)
    check("conflicting form was not learned as alias", "James Brown" not in _aliases(db, "john"))


def test_roster_known_id_requires_matching_occurrence_evidence() -> None:
    print("\n[21e] Known roster ID cannot hijack another player's occurrence")
    roster = [
        _candidate("john-id", "John Smith", "John", "Smith"),
        _candidate("kitsing-id", "Kristjan Kitsing", "Kristjan", "Kitsing"),
    ]
    db, r = _setup_provider_db(roster)
    _seed(db, "john", "John Smith", "John", "Smith")
    _seed(db, "kitsing", "Kristjan Kitsing", "Kristjan", "Kitsing")
    r.store_external_id("john", "fake", "john-id")

    result = r.resolve(_ctx("K. Kitsing"))
    check("resolves Kitsing, not first mapped roster player", result.player_id == "kitsing")
    check("Kitsing provider ID linked", _ext_player_id(db, "fake", "kitsing") == "kitsing-id")
    check("wrong alias not learned", "K. Kitsing" not in _aliases(db, "john"))


def test_provider_jersey_mismatch_requires_review() -> None:
    print("\n[21f] Mismatching jersey weakens an initial-only roster match")
    roster = [_candidate("pid-john", "John Smith", "John", "Smith", jersey_number="11")]
    db, r = _setup_provider_db(roster)
    _seed(db, "john", "John Smith", "John", "Smith")

    raised = False
    try:
        r.resolve(_ctx("J. Smith", jersey="7"))
    except ManualReviewRequired:
        raised = True
    check("jersey mismatch requires review", raised)
    check("provider ID not linked", _ext_player_id(db, "fake", "john") is None)


def test_context_refines_but_never_replaces_name_evidence() -> None:
    print("\n[21g] Context refines plausible names but cannot rescue different names")
    context = IdentityContext(
        raw_name="J. Smith", team_id="TLM", season_id=3, league_id=1,
        jersey_number="7", position="G",
    )
    matching = _candidate(
        "john", "John Smith", "John", "Smith", jersey_number="7",
        position="G", team_id="TLM", season_id=3,
    )
    conflicting_context = _candidate(
        "john", "John Smith", "John", "Smith", jersey_number="11",
        position="F", team_id="OTHER", season_id=4,
    )
    different_name = _candidate(
        "brown", "James Brown", "James", "Brown", jersey_number="7",
        position="G", team_id="TLM", season_id=3,
    )
    check("matching context strengthens name", _score_candidate_with_context(context, matching) > 0.90)
    check("conflicting context weakens name", _score_candidate_with_context(context, conflicting_context) < 0.80)
    check("context cannot promote different name", _score_candidate_with_context(context, different_name) == 0.0)


def test_provider_jersey_context_breaks_initial_ambiguity() -> None:
    print("\n[22] Context-aware roster scoring — jersey breaks initial ambiguity")
    roster = [
        _candidate("pid-john", "John Smith", "John", "Smith", jersey_number="7"),
        _candidate("pid-james", "James Smith", "James", "Smith", jersey_number="11"),
    ]
    db, r = _setup_provider_db(roster)
    _seed(db, "john", "John Smith", "John", "Smith")

    res = r.resolve(_ctx("J. Smith", jersey="7"))
    check("status MATCH",              res.status == ResolverStatus.MATCH)
    check("resolved to John",          res.player_id == "john")
    check("external id linked",        _ext_player_id(db, "fake", "john") == "pid-john")


def test_provider_fallback_a_insufficient_b_resolves() -> None:
    print("\n[23] Sequential providers — A insufficient, B resolves")
    db = _make_db()
    provider_a = NamedFakeProvider(
        "p_a",
        [
            _candidate("a-1", "Completely Different", "Completely", "Different", provider="p_a"),
        ],
    )
    provider_b = NamedFakeProvider(
        "p_b",
        [
            _candidate("b-99", "Kristjan Kitsing", "Kristjan", "Kitsing", provider="p_b"),
        ],
    )
    r = IdentityResolver(db, providers=[provider_a, provider_b])
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")

    r.store_team_external_id("TLM", "p_a", "team-a")
    r.store_season_external_id(3, "p_a", "2025")
    r.store_league_external_id(1, "p_a", "league-a")
    r.store_team_external_id("TLM", "p_b", "team-b")
    r.store_season_external_id(3, "p_b", "2025")
    r.store_league_external_id(1, "p_b", "league-b")

    res = r.resolve(_ctx("K. Kitsing"))
    check("status MATCH",              res.status == ResolverStatus.MATCH)
    check("resolved existing player",  res.player_id == "abc")
    check("linked via provider B",     _ext_player_id(db, "p_b", "abc") == "b-99")
    check("provider A did not force review", True)


def test_local_ambiguity_can_be_resolved_by_provider_context() -> None:
    print("\n[23b] Local ambiguity can be resolved by provider evidence")
    db = _make_db()
    _seed(db, "john", "John Smith", "John", "Smith")
    _seed(db, "james", "James Smith", "James", "Smith")

    provider = NamedFakeProvider(
        "p_b",
        [
            _candidate("b-john", "John Smith", "John", "Smith", provider="p_b", jersey_number="7"),
            _candidate("b-james", "James Smith", "James", "Smith", provider="p_b", jersey_number="11"),
        ],
    )
    resolver = IdentityResolver(db, providers=[provider])
    resolver.store_team_external_id("TLM", "p_b", "team-b")
    resolver.store_season_external_id(3, "p_b", "2025")
    resolver.store_league_external_id(1, "p_b", "league-b")

    result = resolver.resolve(_ctx("J. Smith", jersey="7"))
    check("provider breaks local tie", result.player_id == "john")
    check("no duplicate created", _player_count(db) == 2)
    check("provider ID linked to resolved player", _ext_player_id(db, "p_b", "john") == "b-john")


def test_provider_ambiguous_across_all_providers_requires_review() -> None:
    print("\n[24] Sequential providers — all ambiguous => ManualReviewRequired")
    db = _make_db()
    provider_a = NamedFakeProvider(
        "p_a",
        [
            _candidate("a-john", "John Smith", "John", "Smith", provider="p_a"),
            _candidate("a-james", "James Smith", "James", "Smith", provider="p_a"),
        ],
    )
    provider_b = NamedFakeProvider(
        "p_b",
        [
            _candidate("b-john", "John Smith", "John", "Smith", provider="p_b"),
            _candidate("b-james", "James Smith", "James", "Smith", provider="p_b"),
        ],
    )
    r = IdentityResolver(db, providers=[provider_a, provider_b])
    r.store_team_external_id("TLM", "p_a", "team-a")
    r.store_season_external_id(3, "p_a", "2025")
    r.store_league_external_id(1, "p_a", "league-a")
    r.store_team_external_id("TLM", "p_b", "team-b")
    r.store_season_external_id(3, "p_b", "2025")
    r.store_league_external_id(1, "p_b", "league-b")

    raised = False
    try:
        r.resolve(_ctx("J. Smith"))
    except ManualReviewRequired:
        raised = True

    check("ManualReviewRequired raised", raised)
    check("no player created",           _player_count(db) == 0)


def test_livestats_import_bridge_reuses_source_player_id() -> None:
    """The Node importer bridge must delegate name variants to IdentityResolver."""
    print("\n[25] LiveStats bridge - source ID reuses ARC player across name forms")
    root = Path(__file__).resolve().parents[1]
    bridge = root / "scripts" / "identity" / "resolve_livestats_players.py"
    schema = (root / "database" / "schema.sql").read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as temp_dir:
        database_path = Path(temp_dir) / "arc.db"
        connection = sqlite3.connect(database_path)
        connection.executescript(schema)
        connection.close()

        def resolve(occurrence: dict) -> dict:
            completed = subprocess.run(
                [sys.executable, str(bridge), "--database", str(database_path)],
                input=json.dumps({"occurrences": [occurrence]}),
                text=True,
                capture_output=True,
                check=True,
            )
            return json.loads(completed.stdout)["resolved"][occurrence["key"]]

        first = resolve({
            "key": "first",
            "raw_name": "Kristjan Kitsing",
            "team_id": "team_kalev",
            "season_id": 20252026,
            "league_id": 1,
            "game_id": "game_1",
            "provider": "fiba_livestats",
            "external_player_id": "live-42",
            "jersey_number": "7",
            "position": "F",
        })
        second = resolve({
            "key": "second",
            "raw_name": "K. Kitsing",
            "team_id": "team_kalev",
            "season_id": 20252026,
            "league_id": 1,
            "game_id": "game_2",
            "provider": "fiba_livestats",
            "external_player_id": "live-42",
            "jersey_number": "7",
            "position": "F",
        })

        check("first occurrence created", first["status"] == ResolverStatus.CREATED.value)
        check("alternate name matched by source ID", second["status"] == ResolverStatus.MATCH.value)
        check("same ARC player ID", first["player_id"] == second["player_id"])

        connection = sqlite3.connect(database_path)
        check("one ARC player after repeated occurrence", connection.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1)
        connection.close()


def test_livestats_sequence_name_forms_resolve_to_one_player() -> None:
    print("\n[26] Sequence-level LiveStats forms map to one persistent ARC player")
    db = _make_db()
    resolver = IdentityResolver(db)

    contexts = [
        IdentityContext(raw_name="Kristjan Kitsing", team_id="TLM", season_id=3, league_id=1, jersey_number="7", position="F", provider="fiba_livestats", external_player_id="live-123"),
        IdentityContext(raw_name="K. Kitsing", team_id="TLM", season_id=3, league_id=1, jersey_number="7", position="F", provider="fiba_livestats", external_player_id="live-123"),
        IdentityContext(raw_name="Kitsing, Kristjan", team_id="TLM", season_id=3, league_id=1, jersey_number="7", position="F", provider="fiba_livestats", external_player_id="live-123"),
        IdentityContext(raw_name="Kitsing Kristjan", team_id="TLM", season_id=3, league_id=1, jersey_number="7", position="F", provider="fiba_livestats", external_player_id="live-123"),
        IdentityContext(raw_name="Kristjan Mait Kitsing", team_id="TLM", season_id=3, league_id=1, jersey_number="7", position="F", provider="fiba_livestats", external_player_id="live-123"),
    ]

    results = [resolver.resolve(context) for context in contexts]
    player_ids = {result.player_id for result in results}
    aliases = set(_aliases(db, next(iter(player_ids))))

    check("all occurrences resolved", all(result.player_id is not None for result in results))
    check("single persistent player_id", len(player_ids) == 1)
    check("one player row in database", _player_count(db) == 1)
    check("short and reordered forms learned as aliases", {"K. Kitsing", "Kitsing, Kristjan", "Kitsing Kristjan", "Kristjan Mait Kitsing"}.issubset(aliases))


def test_alias_learning_skips_normalized_duplicates() -> None:
    print("\n[27] Alias learning avoids normalized duplicates")
    db = _make_db()
    _seed(db, "abc", "Kristjan Kitsing", "Kristjan", "Kitsing")
    resolver = IdentityResolver(db)
    resolver.store_external_id("abc", "fiba_livestats", "live-dup")

    resolver.resolve(IdentityContext(
        raw_name="K. Kitsing",
        team_id="TLM",
        season_id=3,
        league_id=1,
        provider="fiba_livestats",
        external_player_id="live-dup",
    ))
    resolver.resolve(IdentityContext(
        raw_name="K Kitsing",
        team_id="TLM",
        season_id=3,
        league_id=1,
        provider="fiba_livestats",
        external_player_id="live-dup",
    ))

    aliases = _aliases(db, "abc")
    check("only one normalized initial alias stored", len(aliases) == 1)
    check("stored alias is meaningful", aliases[0] in {"K. Kitsing", "K Kitsing"})


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_team_change_does_not_duplicate_player()
    test_initial_form_matches_full_name()
    test_comma_format_matches_and_learns_alias()
    test_stored_alias_resolves_directly()
    test_confirmed_alias_is_idempotent()
    test_new_player_is_created()
    test_empty_db_creates_player()
    test_comma_name_creation_keeps_safe_structure()
    test_ambiguous_collision_requires_review()
    test_single_candidate_low_score_creates()
    test_created_result_confidence_is_not_overstated()
    test_external_id_mapping()
    test_update_metadata_fills_nulls_only()
    test_score_name_unit()
    test_score_candidate_name_unit()
    test_parse_name_parts_unit()
    test_split_display_name_unit()
    test_provider_direct_external_id_match()
    test_provider_team_change_same_external_id()
    test_provider_roster_creates_new_player()
    test_provider_roster_links_to_existing_player()
    test_provider_roster_ambiguous_requires_review()
    test_team_external_id_stored_and_retrieved()
    test_season_external_id_stored_and_retrieved()
    test_league_external_id_stored_and_retrieved()
    test_provider_calls_require_all_entity_mappings()
    test_no_provider_mapping_falls_back_to_name_search()
    test_conflicting_arc_player_blocks_creation()
    test_cross_provider_alias_resolution()
    test_last_first_without_comma_matches()
    test_middle_name_variant_matches_existing()
    test_flexible_name_forms_and_normalization()
    test_candidate_lookup_terms_do_not_use_interior_fragments()
    test_diacritic_canonical_discovery_without_existing_ascii_alias()
    test_local_discovery_is_targeted_across_name_forms()
    test_local_history_disambiguates_only_plausible_initials()
    test_local_history_is_neutral_for_normal_team_moves()
    test_conflicting_explicit_middle_names_require_review()
    test_known_source_id_with_conflicting_name_requires_review()
    test_roster_known_id_requires_matching_occurrence_evidence()
    test_provider_jersey_mismatch_requires_review()
    test_context_refines_but_never_replaces_name_evidence()
    test_provider_jersey_context_breaks_initial_ambiguity()
    test_provider_fallback_a_insufficient_b_resolves()
    test_local_ambiguity_can_be_resolved_by_provider_context()
    test_provider_ambiguous_across_all_providers_requires_review()
    test_livestats_import_bridge_reuses_source_player_id()
    test_livestats_sequence_name_forms_resolve_to_one_player()
    test_alias_learning_skips_normalized_duplicates()

    print(f"\n{'=' * 44}")
    print(f"  {_passed} passed   {_failed} failed")
    if _failed:
        sys.exit(1)
