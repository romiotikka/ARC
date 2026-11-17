from __future__ import annotations

import logging
import sqlite3
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from .exceptions import ManualReviewRequired, ProviderCoverageError, ProviderError
from .models import IdentityContext, PlayerCandidate, ResolverResult, ResolverStatus
from .providers.base import IdentityProvider
from .providers.roster_cache import RosterCache
from .utils import normalize_alias

_LOGGER = logging.getLogger(__name__)

# ── Resolution thresholds ─────────────────────────────────────────────────────
#
# Scores are 0.0–1.0; see _score_name() for the complete scale.
#
# _STRONG_MATCH  Auto-match AND learn the incoming name as an alias.
# _MATCH         Auto-match; no alias learning unless external ID confirms identity.
# _MIN_SEP       Minimum gap between the top-2 candidates to suppress ambiguity.
# _REVIEW_FLOOR  Candidates in [_REVIEW_FLOOR, _MATCH) require manual review.

_STRONG_MATCH = 0.90
_MATCH        = 0.75
_MIN_SEP      = 0.15
_REVIEW_FLOOR = 0.50


class IdentityResolver:
    """Coordinator for the ARC Identity v2 resolution flow.

    All identity decisions, database writes, alias management, and external-ID
    persistence live here.  Providers are pure data adapters; they never make
    identity decisions.

    Usage::

        import sqlite3
        from scripts.identity.resolver import IdentityResolver

        db = sqlite3.connect("arc.db")
        resolver = IdentityResolver(db)
        result = resolver.resolve(context)

    The constructor sets ``db.row_factory = sqlite3.Row``.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        providers: Sequence[IdentityProvider] = (),
        roster_cache: RosterCache | None = None,
        provider_priority_by_league: Mapping[int, Sequence[str]] | None = None,
    ) -> None:
        db.row_factory = sqlite3.Row
        db.create_function("normalize_alias", 1, _normalize_alias_sql)
        self._db = db
        self._providers = tuple(providers)
        self._roster_cache = roster_cache
        self._provider_priority_by_league = {
            int(league_id): tuple(provider_names)
            for league_id, provider_names in (provider_priority_by_league or {}).items()
        }
        self._provider_roster_mapping_overrides: dict[
            tuple[str, int, int, int], tuple[str, str, str]
        ] = {}
        self._logged_missing_mappings: set[tuple[str, int, int, int, tuple[str, ...]]] = set()

    @property
    def providers(self) -> tuple[IdentityProvider, ...]:
        """Configured providers in the resolver's lookup order."""
        return self._providers

    def configure_provider_roster_mapping(
        self,
        provider: str,
        league_id: int,
        season_id: int,
        team_id: int,
        external_league_id: str,
        external_season_id: str,
        external_team_id: str,
    ) -> None:
        """Register a context-scoped roster mapping that cannot fit the global ID tables."""
        self._provider_roster_mapping_overrides[
            (provider, league_id, season_id, team_id)
        ] = (external_league_id, external_season_id, external_team_id)

    # ── Public interface ──────────────────────────────────────────────────────

    def resolve(self, context: IdentityContext) -> ResolverResult:
        """Resolve one parser-supplied player occurrence to a persistent ARC player_id.

          Resolution order:

          A. Existing ARC name/alias matching, using context to refine plausible
              candidates. Strong local matches are returned immediately.
          B. Configured provider rosters are consulted when local evidence is
              uncertain or unavailable; ambiguous evidence raises review.
          C. Create a new unverified ARC player when no reliable match is found.
        """
        # A source-player ID that has already been confirmed is stronger than a
        # name form.  LiveStats importers use this path for stable feed IDs.
        if context.external_player_id:
            existing_id = self._find_by_external_id(
                context.provider, context.external_player_id
            )
            if existing_id:
                score = self._score_existing_player(existing_id, context.raw_name, context)
                if score < _MATCH:
                    raise ManualReviewRequired(
                        f"Source external ID {context.provider!r}/"
                        f"{context.external_player_id!r} maps to {existing_id!r}, "
                        f"but its name evidence conflicts with {context.raw_name!r}."
                    )
                self._maybe_add_alias(existing_id, context.raw_name, context.provider)
                return ResolverResult(
                    status=ResolverStatus.MATCH,
                    player_id=existing_id,
                    confidence=1.0,
                    provider=context.provider,
                )

        # Path A: existing ARC name/alias matching.
        candidates = self._search_candidates(context.raw_name, context)
        local_result: ResolverResult | None = None

        if candidates:
            local_result = self._evaluate_candidates(candidates, context)
            if context.ambiguous_source_name_group:
                local_result = None
                local_review_error = ManualReviewRequired(
                    f"Duplicate LiveStats name {context.raw_name!r} requires independent roster or player-ID evidence.",
                    details={"candidate_players": _local_candidate_details(candidates)},
                )
            else:
                local_review_error = None

            if local_result is not None and local_result.confidence >= _STRONG_MATCH:
                self._store_context_external_id(local_result.player_id, context)
                return local_result

            if local_result is None and local_review_error is None:
                top_score = candidates[0]["_score"]
                if top_score >= _REVIEW_FLOOR:
                    local_review_error = ManualReviewRequired(
                        f"Ambiguous identity for {context.raw_name!r}: "
                        f"best score {top_score:.2f} is below the auto-match threshold "
                        "and above the creation floor. Manual review required."
                    )
                else:
                    local_review_error = None
            elif local_result is not None:
                local_review_error = None
        else:
            local_review_error = None

        if context.ambiguous_source_name_group and not context.external_player_id:
            local_result = None
            if local_review_error is None:
                local_review_error = ManualReviewRequired(
                    f"Duplicate LiveStats name {context.raw_name!r} requires independent roster or player-ID evidence.",
                    details={"candidate_players": _local_candidate_details(candidates)},
                )

        if local_result is not None and not self._providers:
            self._store_context_external_id(local_result.player_id, context)
            return local_result

        # Path B: provider roster evidence can resolve local ambiguity, but does
        # not override a strong local match already returned above.
        provider_review_error: ManualReviewRequired | None = None
        if self._providers:
            try:
                result = self._resolve_via_provider(context)
            except ManualReviewRequired as exc:
                provider_review_error = exc
            else:
                if result is not None:
                    self._store_context_external_id(result.player_id, context)
                    return result

        if provider_review_error is not None:
            raise provider_review_error
        if local_review_error is not None:
            raise local_review_error
        if local_result is not None:
            self._store_context_external_id(local_result.player_id, context)
            return local_result

        # Path C: no plausible match anywhere — create a new unverified player.
        player_id = self._create_player(context)
        self._store_context_external_id(player_id, context)
        return ResolverResult(
            status=ResolverStatus.CREATED,
            player_id=player_id,
            confidence=0.0,
        )

    def resolve_by_external_id(
        self, provider: str, external_player_id: str
    ) -> int | None:
        """Return the ARC player_id for a confirmed provider external ID, or None.

        Once an external ID is stored, this is the strongest and cheapest resolution
        path: O(1) lookup, no name matching, no scoring.
        """
        return self._find_by_external_id(provider, external_player_id)

    def store_external_id(
        self, player_id: int, provider: str, external_player_id: str
    ) -> None:
        """Persist a provider → ARC player_id mapping in player_external_ids.

        Idempotent for the same mapping.  A mapping already owned by another
        ARC player is an identity conflict and must be reviewed, never ignored.
        Future resolve_by_external_id() calls will return player_id directly,
        bypassing name-based matching entirely (spec §11).
        """
        existing_id = self._find_by_external_id(provider, external_player_id)
        if existing_id:
            if existing_id != player_id:
                raise ManualReviewRequired(
                    f"External ID {provider!r}/{external_player_id!r} is already "
                    f"mapped to ARC player {existing_id!r}, not {player_id!r}."
                )
            return
        with self._db:
            self._db.execute(
                """
                INSERT INTO player_external_ids
                    (player_id, provider, external_player_id)
                VALUES (?, ?, ?)
                """,
                (player_id, provider, external_player_id),
            )

    def update_player_metadata(
        self, player_id: int, candidate: PlayerCandidate
    ) -> None:
        """Fill NULL metadata fields from a confirmed provider candidate.

        Only touches columns that are currently NULL — never overwrites confirmed
        data with lower-quality information (spec §6).
        Called after identity has been confirmed via external ID or strong match.
        """
        with self._db:
            self._db.execute(
                """
                UPDATE players SET
                    birth_date  = COALESCE(birth_date,  :birth_date),
                    nationality = COALESCE(nationality, :nationality),
                    height_cm   = COALESCE(height_cm,   :height_cm),
                    position    = COALESCE(position,    :position),
                    updated_at  = :now
                WHERE player_id = :player_id
                """,
                {
                    "player_id":   player_id,
                    "birth_date":  candidate.birth_date,
                    "nationality": candidate.nationality,
                    "height_cm":   candidate.height_cm,
                    "position":    candidate.position,
                    "now":         _utcnow(),
                },
            )

    def store_team_external_id(
        self, arc_team_id: int, provider: str, external_team_id: str
    ) -> None:
        """Map an ARC team_id to a provider-native external team ID.

        Idempotent. Once stored, the resolver uses this mapping to fetch provider
        rosters when resolving player occurrences for this team.
        """
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO team_external_ids
                    (team_id, provider, external_team_id)
                VALUES (?, ?, ?)
                """,
                (arc_team_id, provider, external_team_id),
            )

    def store_season_external_id(
        self, arc_season_id: int, provider: str, external_season_id: str
    ) -> None:
        """Map an ARC season_id to a provider-native external season ID.

        Provider season IDs are stored as TEXT strings even when the upstream
        provider represents them as integers (spec §8).
        """
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO season_external_ids
                    (season_id, provider, external_season_id)
                VALUES (?, ?, ?)
                """,
                (arc_season_id, provider, external_season_id),
            )

    def store_league_external_id(
        self, arc_league_id: int, provider: str, external_league_id: str
    ) -> None:
        """Map an ARC league_id to a provider-native external league ID.

        The mapping is deliberately explicit: it records that this provider
        covers this ARC league and supplies provider-only IDs for calls such as
        ``get_teams``.  It never guesses coverage from an ARC ID.
        """
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO league_external_ids
                    (league_id, provider, external_league_id)
                VALUES (?, ?, ?)
                """,
                (arc_league_id, provider, str(external_league_id)),
            )

    # ── Candidate search ──────────────────────────────────────────────────────

    def _search_candidates(
        self, raw_name: str, context: IdentityContext | None = None
    ) -> list[dict]:
        """Return existing players that plausibly match *raw_name*, scored descending.

        Candidate discovery uses the possible edge-name tokens from every name
        profile (normal and reversed), plus exact canonical/alias lookup.  SQL
        only returns a bounded plausible set; Python then performs the detailed
        flexible name and context scoring.  Discovery never decides identity.
        """
        terms = set(_candidate_lookup_terms(raw_name))
        if context is not None:
            for name in _full_source_names(context):
                terms.update(_candidate_lookup_terms(name))
        if not terms and not raw_name.strip():
            return []

        clauses = [
            "p.canonical_name = ? COLLATE NOCASE",
            "pa.alias_name = ? COLLATE NOCASE",
        ]
        params: list[str] = [raw_name, raw_name]
        normalized_raw_name = normalize_alias(raw_name)
        if normalized_raw_name:
            clauses.extend(
                [
                    "normalize_alias(p.canonical_name) = ?",
                    "normalize_alias(pa.alias_name) = ?",
                ]
            )
            params.extend([normalized_raw_name, normalized_raw_name])

        for term in terms:
            normalized_term = normalize_alias(term)
            clauses.extend(
                [
                    "p.last_name = ? COLLATE NOCASE",
                    "p.canonical_name LIKE ? COLLATE NOCASE",
                    "pa.alias_name LIKE ? COLLATE NOCASE",
                ]
            )
            params.extend([term, f"%{term}%", f"%{term}%"])
            if len(normalized_term) >= 2:
                clauses.extend(
                    [
                        "normalize_alias(p.last_name) = ?",
                        "normalize_alias(p.canonical_name) LIKE ?",
                        "normalize_alias(pa.alias_name) LIKE ?",
                    ]
                )
                params.extend(
                    [
                        normalized_term,
                        f"%{normalized_term}%",
                        f"%{normalized_term}%",
                    ]
                )

        rows = self._db.execute(
            f"""
            SELECT DISTINCT
                p.player_id,
                p.canonical_name,
                p.first_name,
                p.last_name,
                p.birth_date,
                p.height_cm,
                p.position,
                p.identity_status
            FROM players p
            LEFT JOIN player_aliases pa ON pa.player_id = p.player_id
            WHERE {' OR '.join(clauses)}
            """,
            params,
        ).fetchall()

        if not rows:
            return []

        candidates = []
        for row in rows:
            p = dict(row)
            p["aliases"] = self._get_aliases(p["player_id"])
            p["_structured_name_malformed"] = _has_malformed_structured_name(p)
            if p["_structured_name_malformed"]:
                p["first_name"] = None
                p["last_name"] = None
            name_score = _score_context_name(raw_name, p, context)
            if name_score <= 0.0:
                continue
            p["_name_score"] = name_score
            p["_score"] = self._score_local_candidate_with_context(
                p, context, name_score
            )
            candidates.append(p)

        candidates.sort(key=lambda x: x["_score"], reverse=True)
        return candidates

    def _score_local_candidate_with_context(
        self,
        player: dict,
        context: IdentityContext | None,
        name_score: float,
    ) -> float:
        """Refine strong local name evidence using prior PlayerGame history.

        Context may separate initial collisions, but never rescues conflicting
        names: only an initial-or-better name score can receive a context boost.
        Missing history is neutral, so ordinary transfers remain valid.
        """
        if context is None or name_score < 0.80:
            return name_score

        score = name_score
        if context.position and player.get("position"):
            score += 0.03 if context.position == player["position"] else -0.03

        try:
            row = self._db.execute(
                """
                SELECT
                    SUM(CASE WHEN pg.team_id = :team_id
                              AND g.season_id = :season_id
                              AND g.league_id = :league_id THEN 1 ELSE 0 END) AS scoped_games,
                    SUM(CASE WHEN pg.team_id = :team_id
                              AND g.season_id = :season_id
                              AND g.league_id = :league_id
                              AND pg.shirt_number = :jersey_number THEN 1 ELSE 0 END) AS jersey_matches,
                    SUM(CASE WHEN pg.team_id = :team_id
                              AND g.season_id = :season_id
                              AND g.league_id = :league_id
                              AND pg.shirt_number IS NOT NULL THEN 1 ELSE 0 END) AS known_jerseys,
                    SUM(CASE WHEN pg.team_id = :team_id
                              AND g.season_id = :season_id
                              AND g.league_id = :league_id
                              AND pg.position = :position THEN 1 ELSE 0 END) AS position_matches
                FROM player_games pg
                JOIN games g ON g.game_id = pg.game_id
                WHERE pg.player_id = :player_id
                """,
                {
                    "player_id": player["player_id"],
                    "team_id": context.team_id,
                    "season_id": context.season_id,
                    "league_id": context.league_id,
                    "jersey_number": context.jersey_number,
                    "position": context.position,
                },
            ).fetchone()
        except sqlite3.OperationalError:
            # Resolver unit embeddings may intentionally omit fact tables.
            return max(0.0, min(1.0, score))

        if row and row["scoped_games"]:
            score += 0.18
            if context.jersey_number and row["jersey_matches"]:
                score += 0.08
            elif context.jersey_number and row["known_jerseys"]:
                score -= 0.08
            if context.position and row["position_matches"]:
                score += 0.03

        return max(0.0, min(1.0, score))

    def _score_existing_player(
        self, player_id: str, raw_name: str, context: IdentityContext | None = None
    ) -> float:
        """Score a source name against one already-known ARC identity."""
        row = self._db.execute(
            """
            SELECT player_id, canonical_name, first_name, last_name,
                   birth_date, height_cm, position, identity_status
            FROM players WHERE player_id = ?
            """,
            (player_id,),
        ).fetchone()
        if row is None:
            return 0.0
        player = dict(row)
        player["aliases"] = self._get_aliases(player_id)
        return _score_context_name(raw_name, player, context)

    def _get_aliases(self, player_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT alias_name FROM player_aliases WHERE player_id = ?",
            (player_id,),
        ).fetchall()
        return [r["alias_name"] for r in rows]

    def _find_by_external_id(
        self, provider: str, external_player_id: str
    ) -> int | None:
        row = self._db.execute(
            """
            SELECT player_id FROM player_external_ids
            WHERE provider = ? AND external_player_id = ?
            """,
            (provider, external_player_id),
        ).fetchone()
        return _coerce_arc_player_id(row["player_id"]) if row else None

    def _lookup_team_external_id(
        self, provider: str, arc_team_id: int
    ) -> str | None:
        """Return the provider's external team ID for an ARC team, or None."""
        row = self._db.execute(
            """
            SELECT external_team_id FROM team_external_ids
            WHERE provider = ? AND team_id = ?
            """,
            (provider, arc_team_id),
        ).fetchone()
        return row["external_team_id"] if row else None

    def _lookup_season_external_id(
        self, provider: str, arc_season_id: int
    ) -> str | None:
        """Return the provider's external season ID for an ARC season, or None."""
        row = self._db.execute(
            """
            SELECT external_season_id FROM season_external_ids
            WHERE provider = ? AND season_id = ?
            """,
            (provider, arc_season_id),
        ).fetchone()
        return row["external_season_id"] if row else None

    def _lookup_league_external_id(
        self, provider: str, arc_league_id: int
    ) -> str | None:
        """Return the provider's external league ID for an ARC league, or None."""
        row = self._db.execute(
            """
            SELECT external_league_id FROM league_external_ids
            WHERE provider = ? AND league_id = ?
            """,
            (provider, arc_league_id),
        ).fetchone()
        return row["external_league_id"] if row else None

    # ── Candidate evaluation ──────────────────────────────────────────────────

    def _evaluate_candidates(
        self, candidates: list[dict], context: IdentityContext
    ) -> ResolverResult | None:
        """Return a MATCH result if the best candidate meets the threshold.

        Returns None when no candidate qualifies; the caller decides between
        ManualReviewRequired and player creation.

        Rules:
        - Score < _MATCH: never auto-match.
        - Multiple candidates with separation < _MIN_SEP: do not force a match.
        - Score >= _STRONG_MATCH: match AND learn the incoming name as an alias.
        """
        best = candidates[0]
        best_score = best["_score"]
        second_score = candidates[1]["_score"] if len(candidates) > 1 else 0.0
        separation = best_score - second_score
        has_strong_full_source_match = bool(
            context
            and any(
                _score_name(full_name, best) >= _STRONG_MATCH
                for full_name in _full_source_names(context)
            )
        )

        if best_score < _MATCH:
            return None

        if best_score < _STRONG_MATCH and not has_strong_full_source_match:
            return None

        if len(candidates) > 1 and separation < _MIN_SEP:
            return None

        if best_score >= _STRONG_MATCH:
            self._maybe_add_alias(best["player_id"], context.raw_name, context.provider)

        return ResolverResult(
            status=ResolverStatus.MATCH,
            player_id=best["player_id"],
            confidence=best_score,
        )

    # ── Provider-assisted resolution ───────────────────────────────────────────

    def _resolve_via_provider(
        self, context: IdentityContext
    ) -> ResolverResult | None:
        """Fetch a provider roster and resolve the player occurrence within it.

        Returns a ResolverResult on a confident match.
        Raises ManualReviewRequired when the roster yields ambiguous candidates.
        Returns None when no provider has a mapping for this team/season, so the
        caller falls through to name-only resolution.
        """
        provider_order = self._provider_priority_by_league.get(context.league_id)
        if provider_order:
            return self._resolve_via_prioritized_providers(context, provider_order)
        return self._resolve_provider_candidates(
            context, self._score_provider_rosters(context, self._iter_provider_rosters(context))
        )

    def _resolve_via_prioritized_providers(
        self, context: IdentityContext, provider_order: Sequence[str]
    ) -> ResolverResult | None:
        providers = {provider.provider_name: provider for provider in self._providers}
        deferred: list[tuple[PlayerCandidate, float, int | str | None]] = []
        basket_candidates: list[tuple[PlayerCandidate, float, int | str | None]] = []

        for provider_name in provider_order:
            provider = providers.get(provider_name)
            if provider is None:
                continue
            rosters = self._iter_provider_rosters(context, providers=(provider,))
            scored = self._score_provider_rosters(context, rosters)
            if not scored:
                continue

            if provider_name == "basket":
                self._review_conflicting_provider_jersey(context, scored)
                basket_candidates = scored
                if self._has_confident_provider_candidate(context, scored):
                    return self._resolve_provider_candidates(context, scored)
                continue

            if basket_candidates and self._providers_disagree(basket_candidates, scored):
                raise ManualReviewRequired(
                    "Basket.ee and API-Sports rosters provide conflicting player evidence.",
                    details={
                        "candidate_information": self._provider_candidate_details(
                            basket_candidates + scored
                        )
                    },
                )
            if basket_candidates:
                # API-Sports is a fallback: use it only after Basket has no
                # confident candidate and its evidence does not contradict Basket.
                return self._resolve_provider_candidates(context, scored)
            if self._has_confident_provider_candidate(context, scored):
                return self._resolve_provider_candidates(context, scored)
            deferred.extend(scored)

        if basket_candidates:
            return self._resolve_provider_candidates(context, basket_candidates)
        if deferred:
            return self._resolve_provider_candidates(context, deferred)
        return None

    def _score_provider_rosters(
        self,
        context: IdentityContext,
        rosters: Sequence[tuple[str, tuple[PlayerCandidate, ...]]],
    ) -> list[tuple[PlayerCandidate, float, int | str | None]]:
        grouped: dict[tuple[object, ...], tuple[PlayerCandidate, float, int | str | None]] = {}
        for _, roster in rosters:
            for candidate in roster:
                score = _score_candidate_with_context(context, candidate)
                if score < _REVIEW_FLOOR:
                    continue
                arc_id = self._find_by_external_id(candidate.provider, candidate.external_player_id)
                key = ("arc", arc_id) if arc_id is not None else (
                    "external", candidate.provider, candidate.external_player_id
                )
                if key not in grouped or score > grouped[key][1]:
                    grouped[key] = (candidate, score, arc_id)
        return sorted(grouped.values(), key=lambda item: item[1], reverse=True)

    def _resolve_provider_candidates(
        self,
        context: IdentityContext,
        scored: Sequence[tuple[PlayerCandidate, float, int | str | None]],
    ) -> ResolverResult | None:
        if not scored:
            return None
        self._review_conflicting_provider_jersey(context, scored)

        confirmed_ids: dict[int | str, tuple[PlayerCandidate, float]] = {}
        for candidate, score, arc_id in scored:
            if arc_id is None:
                continue
            name_score = self._score_existing_player(arc_id, context.raw_name, context)
            if name_score >= _MATCH:
                confirmed_ids[arc_id] = (candidate, score)
        if len(confirmed_ids) > 1:
            raise ManualReviewRequired(
                f"Multiple confirmed provider IDs have strong name evidence for {context.raw_name!r}."
            )
        if confirmed_ids:
            arc_id, (candidate, _) = next(iter(confirmed_ids.items()))
            self._maybe_add_alias(arc_id, context.raw_name, context.provider)
            self.update_player_metadata(arc_id, candidate)
            return ResolverResult(
                status=ResolverStatus.MATCH,
                player_id=arc_id,
                confidence=1.0,
                provider=candidate.provider,
            )

        confident = [
            item for item in scored if self._provider_candidate_is_confident(context, item[0], item[1])
        ]
        if len(confident) > 1:
            raise ManualReviewRequired(
                f"Multiple provider roster candidates have strong evidence for {context.raw_name!r}.",
                details={"candidate_information": self._provider_candidate_details(confident)},
            )

        if confident:
            best_candidate, best_score, arc_id = confident[0]
            if arc_id:
                if self._score_existing_player(arc_id, context.raw_name, context) < _MATCH:
                    raise ManualReviewRequired(
                        f"Provider ID {best_candidate.provider!r}/"
                        f"{best_candidate.external_player_id!r} maps to {arc_id!r}, "
                        f"but its ARC name evidence conflicts with {context.raw_name!r}."
                    )
                self._maybe_add_alias(arc_id, context.raw_name, context.provider)
                self.update_player_metadata(arc_id, best_candidate)
                return ResolverResult(
                    status=ResolverStatus.MATCH,
                    player_id=arc_id,
                    confidence=1.0,
                    provider=best_candidate.provider,
                )
            return self._confirm_from_roster(best_candidate, best_score, context)

        best_score = scored[0][1]
        second_score = scored[1][1] if len(scored) > 1 else 0.0
        separation = best_score - second_score
        raise ManualReviewRequired(
            f"Ambiguous provider roster match for {context.raw_name!r} after "
            f"trying all configured providers (score {best_score:.2f}, "
            f"separation {separation:.2f}). Manual review required.",
            details={"candidate_information": self._provider_candidate_details(scored)},
        )

    def _has_confident_provider_candidate(
        self,
        context: IdentityContext,
        scored: Sequence[tuple[PlayerCandidate, float, int | str | None]],
    ) -> bool:
        confident = [
            item for item in scored if self._provider_candidate_is_confident(context, item[0], item[1])
        ]
        return bool(confident)

    def _provider_candidate_is_confident(
        self, context: IdentityContext, candidate: PlayerCandidate, score: float
    ) -> bool:
        full_name_score = max(
            (_score_candidate_name(name, candidate) for name in _full_source_names(context)),
            default=_score_candidate_name(context.raw_name, candidate),
        )
        has_matching_jersey = bool(
            context.jersey_number
            and candidate.jersey_number
            and context.jersey_number.strip() == candidate.jersey_number.strip()
        )
        return score >= _MATCH and (
            full_name_score >= _STRONG_MATCH
            or (has_matching_jersey and full_name_score >= _REVIEW_FLOOR)
        )

    def _providers_disagree(
        self,
        basket: Sequence[tuple[PlayerCandidate, float, int | str | None]],
        api: Sequence[tuple[PlayerCandidate, float, int | str | None]],
    ) -> bool:
        basket_candidates = [item[0] for item in basket if item[1] >= _MATCH]
        api_candidates = [item[0] for item in api if item[1] >= _MATCH]
        if not basket_candidates or not api_candidates:
            return False
        return not any(
            _score_candidate_name(basket_candidate.canonical_name or "", api_candidate)
            >= _MATCH
            for basket_candidate in basket_candidates
            for api_candidate in api_candidates
        )

    def _review_conflicting_provider_jersey(
        self,
        context: IdentityContext,
        scored: Sequence[tuple[PlayerCandidate, float, int | str | None]],
    ) -> None:
        if not context.jersey_number:
            return
        named = [
            (candidate, _score_candidate_name(name, candidate))
            for candidate, _, _ in scored
            for name in (_full_source_names(context) or (context.raw_name,))
        ]
        for named_candidate, name_score in named:
            if name_score < _STRONG_MATCH or not named_candidate.jersey_number:
                continue
            if str(named_candidate.jersey_number).strip() == context.jersey_number.strip():
                continue
            jersey_candidates = [
                candidate
                for candidate, candidate_name_score in named
                if candidate.external_player_id != named_candidate.external_player_id
                and candidate.jersey_number
                and str(candidate.jersey_number).strip() == context.jersey_number.strip()
                and candidate_name_score >= _REVIEW_FLOOR
            ]
            raise ManualReviewRequired(
                f"{named_candidate.provider} full-name evidence conflicts with the LiveStats jersey number for {context.raw_name!r}.",
                details={
                    "candidate_information": self._provider_candidate_details(
                        [
                            (named_candidate, name_score, None),
                            *[(candidate, _REVIEW_FLOOR, None) for candidate in jersey_candidates],
                        ]
                    )
                },
            )

    def _provider_candidate_details(
        self,
        scored: Sequence[tuple[PlayerCandidate, float, int | str | None]],
    ) -> list[dict[str, object]]:
        return [
            {
                "provider": candidate.provider,
                "external_player_id": candidate.external_player_id,
                "canonical_name": candidate.canonical_name,
                "first_name": candidate.first_name,
                "last_name": candidate.last_name,
                "team_id": candidate.team_id,
                "season_id": candidate.season_id,
                "jersey_number": candidate.jersey_number,
                "position": candidate.position,
                "confidence": score,
            }
            for candidate, score, _ in scored
        ]

    def _iter_provider_rosters(
        self,
        context: IdentityContext,
        providers: Sequence[IdentityProvider] | None = None,
    ) -> tuple[tuple[str, tuple[PlayerCandidate, ...]], ...]:
        """Return all provider rosters available for this ARC team/season context.

        Iterates configured providers in order and yields every successful roster.
        Missing mappings and provider failures are diagnosed and cached separately
        so they cannot be mistaken for an empty roster.
        """
        rosters: list[tuple[str, tuple[PlayerCandidate, ...]]] = []
        for provider in providers if providers is not None else self._providers:
            override = self._provider_roster_mapping_overrides.get(
                (provider.provider_name, context.league_id, context.season_id, context.team_id)
            )
            if override:
                ext_league, ext_season, ext_team = override
            else:
                ext_team = self._lookup_team_external_id(
                    provider.provider_name, context.team_id
                )
                ext_season = self._lookup_season_external_id(
                    provider.provider_name, context.season_id
                )
                ext_league = self._lookup_league_external_id(
                    provider.provider_name, context.league_id
                )
            # A provider is considered applicable only when all entity mappings
            # for this occurrence are explicitly configured.  The roster API
            # accepts team/season only; league mapping is the coverage gate and
            # remains available for provider calls that require a league ID.
            missing = tuple(
                name
                for name, value in (
                    ("league", ext_league),
                    ("season", ext_season),
                    ("team", ext_team),
                )
                if not value
            )
            if missing:
                warning_key = (
                    provider.provider_name,
                    context.league_id,
                    context.season_id,
                    context.team_id,
                    missing,
                )
                if warning_key not in self._logged_missing_mappings:
                    _LOGGER.warning(
                        "Provider roster skipped: provider=%s missing=%s arc_league=%s arc_season=%s arc_team=%s",
                        provider.provider_name,
                        ",".join(missing),
                        context.league_id,
                        context.season_id,
                        context.team_id,
                    )
                    self._logged_missing_mappings.add(warning_key)
                continue

            cache_key = (
                provider.provider_name,
                ext_league,
                ext_season,
                ext_team,
            )
            cached = (
                self._roster_cache.get(*cache_key)
                if self._roster_cache is not None
                else None
            )
            if cached is not None:
                _LOGGER.debug(
                    "Provider roster cache hit: provider=%s league=%s season=%s team=%s status=%s",
                    *cache_key,
                    cached.status,
                )
                if cached.status in ("success", "empty"):
                    rosters.append((provider.provider_name, cached.roster))
                else:
                    _LOGGER.warning(
                        "Cached provider roster unavailable: provider=%s league=%s season=%s team=%s status=%s error=%s",
                        *cache_key,
                        cached.status,
                        cached.error,
                    )
                continue

            try:
                roster = provider.get_season_roster(ext_team, ext_season)
            except ProviderCoverageError as exc:
                if self._roster_cache is not None:
                    self._roster_cache.put(*cache_key, "unsupported", error=str(exc))
                _LOGGER.warning(
                    "Provider roster coverage unavailable: provider=%s league=%s season=%s team=%s error=%s",
                    *cache_key,
                    exc,
                )
                continue
            except ProviderError as exc:
                if self._roster_cache is not None:
                    self._roster_cache.put(*cache_key, "error", error=str(exc))
                _LOGGER.warning(
                    "Provider roster request failed: provider=%s league=%s season=%s team=%s error=%s",
                    *cache_key,
                    exc,
                )
                continue

            status = "success" if roster else "empty"
            if self._roster_cache is not None:
                self._roster_cache.put(*cache_key, status, roster=roster)
            _LOGGER.info(
                "Provider roster retrieved: provider=%s league=%s season=%s team=%s status=%s players=%s",
                *cache_key,
                status,
                len(roster),
            )
            rosters.append((provider.provider_name, roster))
        return tuple(rosters)

    def _confirm_from_roster(
        self,
        candidate: PlayerCandidate,
        best_score: float,
        context: IdentityContext,
    ) -> ResolverResult:
        """Link a name-matched roster candidate to an ARC player, creating one if needed.

        Search ARC for an existing player matching the candidate's canonical name.
        If found confidently, store the external ID on that player (MATCH).
        If ARC has a similar-but-not-confident player, raise ManualReviewRequired
        to prevent creating a duplicate.
        If ARC has no similar player, create a new player from provider data (CREATED).
        """
        search_name = candidate.canonical_name or context.raw_name
        arc_candidates = self._search_candidates(search_name, context)

        player_id: int | None = None
        strong_local = [item for item in arc_candidates if item["_score"] >= _MATCH]
        if len(strong_local) > 1:
            raise ManualReviewRequired(
                f"Provider confirms {candidate.canonical_name!r}, but multiple ARC identities have strong name evidence.",
                details={"candidate_players": _local_candidate_details(strong_local)},
            )
        if strong_local:
            player_id = strong_local[0]["player_id"]
        else:
            supported_local = [
                item
                for item in arc_candidates
                if item["_score"] >= _REVIEW_FLOOR
                and self._has_reliable_local_conflict(item, context)
            ]
            if supported_local:
                top = supported_local[0]
                raise ManualReviewRequired(
                    f"Provider confirms {candidate.canonical_name!r} (ext_id "
                    f"{candidate.external_player_id!r}) but ARC has a context-supported "
                    f"identity {top['canonical_name']!r} (score {top['_score']:.2f}). "
                    "Manual review required to prevent duplicate creation.",
                    details={"candidate_players": _local_candidate_details(supported_local)},
                )

        if player_id:
            status = ResolverStatus.MATCH
        else:
            # No similar ARC player — this is a genuinely new player.
            player_id = self._create_player_from_candidate(candidate, context)
            status = ResolverStatus.CREATED

        # External ID confirmed: persist the link then learn the incoming name variant.
        self.store_external_id(player_id, candidate.provider, candidate.external_player_id)
        self._maybe_add_alias(player_id, context.raw_name, context.provider)
        self.update_player_metadata(player_id, candidate)

        return ResolverResult(
            status=status,
            player_id=player_id,
            confidence=best_score,
            provider=candidate.provider,
        )

    def _has_reliable_local_conflict(
        self, player: dict, context: IdentityContext
    ) -> bool:
        if player.get("identity_status") in ("verified", "conflicted"):
            return True
        try:
            row = self._db.execute(
                """
                SELECT 1
                FROM player_games pg
                JOIN games g ON g.game_id = pg.game_id
                WHERE pg.player_id = :player_id
                  AND pg.team_id = :team_id
                  AND g.season_id = :season_id
                  AND g.league_id = :league_id
                  AND (:jersey_number IS NULL OR pg.shirt_number = :jersey_number)
                LIMIT 1
                """,
                {
                    "player_id": player["player_id"],
                    "team_id": context.team_id,
                    "season_id": context.season_id,
                    "league_id": context.league_id,
                    "jersey_number": context.jersey_number,
                },
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def _create_player_from_candidate(
        self, candidate: PlayerCandidate, context: IdentityContext
    ) -> int:
        """Create a new ARC player using the provider's richer identity data.

        Prefers complete provider profile names and never stores an initial as a
        complete given name or surname when fuller context is available.
        """
        player_id = self._next_player_id()
        candidate_name = candidate.canonical_name
        candidate_first, candidate_last = _split_display_name(candidate_name or "")
        candidate_is_full = bool(
            _looks_like_full_name(candidate_name or "")
            and candidate_first
            and candidate_last
            and not _is_initial_like(candidate_first)
            and not _is_initial_like(candidate_last)
        )
        source_is_full = bool(
            context.first_name
            and context.last_name
            and not _is_initial_like(context.first_name)
            and not _is_initial_like(context.last_name)
        )

        if candidate_is_full:
            source_name = (
                f"{context.first_name.strip()} {context.last_name.strip()}"
                if source_is_full
                else None
            )
            if source_name and _same_normalized_name_tokens(source_name, candidate_name or ""):
                first_name = context.first_name.strip()
                last_name = context.last_name.strip()
                canonical = f"{first_name} {last_name}"
            else:
                first_name = candidate_first
                last_name = candidate_last
                canonical = f"{first_name} {last_name}" if "," in (candidate_name or "") else candidate_name.strip()
        elif source_is_full:
            first_name = context.first_name.strip()
            last_name = context.last_name.strip()
            canonical = f"{first_name} {last_name}"
        elif candidate_is_full:
            canonical = candidate_name.strip()
            first_name = candidate.first_name or candidate_first
            last_name = candidate.last_name or candidate_last
        else:
            canonical = _preferred_canonical_name(context, candidate_name)
            first_name = candidate_first if candidate_is_full else None
            last_name = candidate_last if candidate_is_full else None
        now = _utcnow()
        with self._db:
            self._db.execute(
                """
                INSERT INTO players
                    (player_id, canonical_name, first_name, last_name,
                     birth_date, nationality, height_cm, position,
                     identity_status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,'unverified',?,?)
                """,
                (
                    player_id, canonical, first_name, last_name,
                    candidate.birth_date, candidate.nationality,
                    candidate.height_cm, candidate.position,
                    now, now,
                ),
            )
        return player_id

    # ── Write operations ──────────────────────────────────────────────────────

    def _create_player(self, context: IdentityContext) -> int:
        """Insert a new unverified player row and return the generated player_id."""
        player_id = self._next_player_id()
        canonical_name = _preferred_canonical_name(context)
        if context.first_name and context.last_name:
            first_name, last_name = context.first_name, context.last_name
        elif "," in canonical_name:
            first_name, last_name = _split_display_name(canonical_name)
        else:
            first_name, last_name = None, None
        now = _utcnow()
        with self._db:
            self._db.execute(
                """
                INSERT INTO players
                    (player_id, canonical_name, first_name, last_name,
                     position, identity_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'unverified', ?, ?)
                """,
                (
                    player_id,
                    canonical_name,
                    first_name,
                    last_name,
                    context.position,
                    now,
                    now,
                ),
            )
        return player_id

    def _next_player_id(self) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(CAST(player_id AS INTEGER)), 0) + 1 AS next_id FROM players"
        ).fetchone()
        return int(row["next_id"])

    def _store_context_external_id(
        self, player_id: int | None, context: IdentityContext
    ) -> None:
        """Persist a confirmed source identifier after a successful resolution."""
        if player_id and context.external_player_id:
            self.store_external_id(
                player_id, context.provider, context.external_player_id
            )

    def _maybe_add_alias(
        self, player_id: int, raw_name: str, source: str
    ) -> None:
        """Store *raw_name* as an alias for *player_id* if it is not already the
        canonical name and has not been stored before.

        Only called when score >= _STRONG_MATCH, satisfying the spec requirement
        that name similarity alone must never automatically create an alias.
        """
        normalized_raw_name = normalize_alias(raw_name)
        if not normalized_raw_name:
            return

        row = self._db.execute(
            "SELECT canonical_name FROM players WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        if row and normalize_alias(row["canonical_name"]) == normalized_raw_name:
            return  # incoming name IS the canonical name; no alias entry needed

        existing_aliases = self._db.execute(
            "SELECT alias_name FROM player_aliases WHERE player_id = ?",
            (player_id,),
        ).fetchall()
        if any(normalize_alias(alias["alias_name"]) == normalized_raw_name for alias in existing_aliases):
            return

        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO player_aliases
                    (player_id, alias_name, source)
                VALUES (?, ?, ?)
                """,
                (player_id, raw_name, source),
            )


# ── Name utilities (module-level for testability) ─────────────────────────────

def _has_malformed_structured_name(player: dict) -> bool:
    first = str(player.get("first_name") or "").strip()
    last = str(player.get("last_name") or "").strip()
    canonical = str(player.get("canonical_name") or "").strip()
    if not first or not last:
        return False
    if _is_initial_like(first) or _is_initial_like(last):
        return True
    canonical_first, canonical_last = _split_display_name(canonical)
    if not canonical_first or not canonical_last:
        return False
    structured = (normalize_alias(first), normalize_alias(last))
    canonical_parts = (normalize_alias(canonical_first), normalize_alias(canonical_last))
    return structured not in (canonical_parts, tuple(reversed(canonical_parts)))


def _local_candidate_details(candidates: Sequence[dict]) -> list[dict[str, object]]:
    return [
        {
            "player_id": candidate["player_id"],
            "canonical_name": candidate["canonical_name"],
            "first_name": candidate.get("first_name"),
            "last_name": candidate.get("last_name"),
            "position": candidate.get("position"),
            "identity_status": candidate.get("identity_status"),
            "confidence": candidate["_score"],
            "structured_name_malformed": candidate.get("_structured_name_malformed", False),
        }
        for candidate in candidates
    ]


def _last_name_token(raw_name: str) -> str:
    """Return the last-name token used to seed the candidate DB search.

    Handles 'First Last', 'F. Last', and 'Last, First' formats.
    The full scoring is done in Python afterward; this is just a broad-net key.
    """
    raw_name = raw_name.strip()
    if not raw_name:
        return ""
    if "," in raw_name:
        return raw_name.split(",", 1)[0].strip()
    return raw_name.split()[-1]


def _candidate_lookup_terms(raw_name: str) -> tuple[str, ...]:
    """Return a small set of raw/normalized edge tokens for SQL discovery.

    The normal and reversed profiles contribute both possible surname positions;
    raw tokens retain diacritics for direct alias lookup, while normalized tokens
    find ASCII canonical forms of the same observed name.
    """
    terms: set[str] = set()
    for profile in _name_profiles(raw_name):
        last = profile["last"]
        if isinstance(last, str) and len(last) >= 2:
            terms.add(last)
    for token in re.findall(r"[^\s,./-]+", raw_name):
        if len(token) >= 2:
            terms.add(token)
            normalized = normalize_alias(token)
            if len(normalized) >= 2:
                terms.add(normalized)
    return tuple(sorted(terms))


def _parse_name_parts(name: str) -> tuple[str | None, str | None]:
    """Return (normalized_first, normalized_last) from a display name string.

    Handles 'First Last', 'F. Last', 'Last, First', and bare 'Last'.
    For two-token names, also tolerates reversed order by preferring the variant
    where the first token is more likely a given name.
    Returns normalized tokens (lowercase, no diacritics, no punctuation) so
    that comparisons are consistent regardless of source encoding.
    """
    profiles = _name_profiles(name)
    if not profiles:
        return None, None
    profile = profiles[0]
    first = profile["given_tokens"][0] if profile["given_tokens"] else None
    return first, profile["last"]


def _name_profiles(name: str) -> list[dict[str, list[str] | str | bool | None]]:
    """Build plausible normalized name profiles from one display string.

    Returns dictionaries with:
    - ``last``: normalized surname token
    - ``given_tokens``: normalized given-name tokens (can include initials)
    - ``reversed_order``: whether this profile assumes a Last First display

    Non-comma names additionally produce a reversed profile to handle feeds
    that emit ``Last First`` (including forms with a middle name) without a
    comma.  Profiles are possibilities for matching, not assertions about a
    canonical display order.
    """
    raw = name.strip()
    if not raw:
        return []

    if "," in raw:
        left, right = [p.strip() for p in raw.split(",", 1)]
        last = normalize_alias(left) or None
        given_tokens = _normalize_token_list(right)
        profiles: list[dict[str, list[str] | str | bool | None]] = [
            {"last": last, "given_tokens": given_tokens, "reversed_order": False}
        ]
        profiles.extend(
            {"last": component, "given_tokens": given_tokens, "reversed_order": False}
            for component in _normalize_token_list(left)
            if component and component != last
        )
        return profiles

    tokens = raw.split()
    if len(tokens) == 1:
        return [{"last": normalize_alias(tokens[0]) or None, "given_tokens": [], "reversed_order": False}]

    given_tokens = _normalize_token_list(" ".join(tokens[:-1]))
    last = normalize_alias(tokens[-1]) or None
    profiles: list[dict[str, list[str] | str | bool | None]] = [
        {"last": last, "given_tokens": given_tokens, "reversed_order": False}
    ]
    profiles.extend(
        {"last": component, "given_tokens": given_tokens, "reversed_order": False}
        for component in _normalize_token_list(tokens[-1])
        if component and component != last
    )

    if len(tokens) >= 2:
        rev_given_tokens = _normalize_token_list(" ".join(tokens[1:]))
        rev_last = normalize_alias(tokens[0]) or None
        profiles.append({"last": rev_last, "given_tokens": rev_given_tokens, "reversed_order": True})

    return profiles


def _normalize_token_list(text: str) -> list[str]:
    """Normalize and split name text into comparable tokens.

    Keeps initials as single-letter tokens and strips punctuation separators.
    """
    tokens = re.split(r"[\s\-./]+", text.strip())
    normalized = [normalize_alias(token) for token in tokens if token]
    return [token for token in normalized if token]


def _same_normalized_name_tokens(first: str, second: str) -> bool:
    first_tokens = _normalize_token_list(first)
    second_tokens = _normalize_token_list(second)
    return bool(first_tokens and sorted(first_tokens) == sorted(second_tokens))


def _score_given_tokens(inc: list[str], ex: list[str]) -> float:
    """Score normalized given-name token lists when surnames already match."""
    if not inc and not ex:
        return 0.60
    if not inc or not ex:
        return 0.55

    inc0 = inc[0]
    ex0 = ex[0]
    if inc0 == ex0:
        if _conflicting_explicit_middle_names(inc, ex):
            return 0.65
        return 0.90

    # Initial-aware checks (e.g., 'K.' vs 'Kristjan', or middle-initial forms).
    inc_initials = {tok[0] for tok in inc if tok}
    ex_initials = {tok[0] for tok in ex if tok}
    if inc0 and ex0 and (len(inc0) == 1 or len(ex0) == 1):
        if inc0[0] == ex0[0]:
            return 0.80
    # Prefix similarity covers practical truncations (e.g., Krist vs Kristjan)
    # while staying below auto-match thresholds.
    if len(inc0) >= 4 and ex0.startswith(inc0):
        return 0.72
    if len(ex0) >= 4 and inc0.startswith(ex0):
        return 0.72

    return 0.50


def _conflicting_explicit_middle_names(inc: list[str], ex: list[str]) -> bool:
    """Return true only for conflicting non-initial middle-name evidence."""
    incoming = [token for token in inc[1:] if len(token) > 1]
    existing = [token for token in ex[1:] if len(token) > 1]
    if not incoming or not existing:
        return False
    return not bool(set(incoming).intersection(existing))


def _score_first_names(inc: str | None, ex: str | None) -> float:
    """Score two normalized first-name tokens against each other.

    Returns:
        0.90  exact match
        0.80  one or both sides is a single-character initial that agrees
        0.60  last-name-only match — neither side has a first name
        0.55  one side is missing a first name
        0.50  both sides have first names that do not match
    """
    if not inc and not ex:
        return 0.60
    if not inc or not ex:
        return 0.55
    if inc == ex:
        return 0.90
    # Initial check: one or both sides reduce to a single character.
    inc_base = inc.replace(".", "")
    ex_base = ex.replace(".", "")
    if inc_base and ex_base and (len(inc_base) <= 1 or len(ex_base) <= 1):
        if inc_base[0] == ex_base[0]:
            return 0.80
    return 0.50


def _score_name(raw_name: str, player: dict) -> float:
    """Return a 0.0–1.0 confidence score for *raw_name* matching *player*.

    Score scale:
        1.00  exact normalized match against the canonical name
        0.95  exact normalized match against a stored alias
        0.90  last-name match + exact full first name (via DB fields or alias)
        0.80  last-name match + matching initial
        0.60  last-name match, neither side has a first name component
        0.55  last-name match, one side is missing a first name
        0.50  last-name match, but first names conflict
        0.00  no meaningful last-name match
    """
    raw_norm = normalize_alias(raw_name)
    raw_tokens = _normalize_token_list(raw_name)

    if raw_norm and raw_tokens == _normalize_token_list(player["canonical_name"]):
        return 1.0

    for alias in player["aliases"]:
        if raw_norm and raw_tokens == _normalize_token_list(alias):
            return 0.95

    incoming_profiles = _name_profiles(raw_name)
    if not incoming_profiles:
        return 0.0

    existing_profiles: list[dict[str, list[str] | str | bool | None]] = []

    ex_last = normalize_alias(player.get("last_name") or "") or None
    ex_first_tokens = _normalize_token_list(player.get("first_name") or "")
    if ex_last:
        existing_profiles.append({"last": ex_last, "given_tokens": ex_first_tokens, "reversed_order": False})

    canonical_profiles = _name_profiles(player.get("canonical_name") or "")
    existing_profiles.extend(canonical_profiles)

    # Aliases are observed forms, not additional structured identity evidence.
    # An exact normalized alias is handled above; allowing a short alias such as
    # "K. Kitsing" to fuzzy-match "Kris Kitsing" would turn a weak initial into
    # an unsafe automatic merge.

    best = 0.0
    for inc in incoming_profiles:
        inc_last = inc["last"]
        if not inc_last:
            continue
        inc_given = inc["given_tokens"]
        for ex in existing_profiles:
            ex_last_p = ex["last"]
            if not ex_last_p or inc_last != ex_last_p:
                continue
            score = _score_given_tokens(inc_given, ex["given_tokens"])
            if (inc.get("reversed_order") or ex.get("reversed_order")) and score < 0.72:
                continue
            if score > best:
                best = score

    return best


def _full_source_names(context: IdentityContext) -> tuple[str, ...]:
    names: list[str] = []
    if context.first_name and context.last_name:
        first = context.first_name.strip()
        last = context.last_name.strip()
        if first and last and not _is_initial_like(first):
            names.append(f"{first} {last}")
    if context.canonical_name_hint and _looks_like_full_name(context.canonical_name_hint):
        names.append(context.canonical_name_hint)
    return tuple(dict.fromkeys(names))


def _score_context_name(
    raw_name: str, player: dict, context: IdentityContext | None
) -> float:
    raw_score = _score_name(raw_name, player)
    if context is None:
        return raw_score
    for full_name in _full_source_names(context):
        full_score = _score_name(full_name, player)
        if raw_score < _MATCH or full_score < _MATCH:
            if max(raw_score, full_score) >= _MATCH:
                raw_score = max(_REVIEW_FLOOR, min(raw_score, full_score))
            else:
                raw_score = min(raw_score, full_score)
        elif full_score >= _STRONG_MATCH:
            raw_score = max(raw_score, full_score)
        else:
            raw_score = min(raw_score, full_score)
    return raw_score


def _candidate_player_dict(candidate: PlayerCandidate) -> dict[str, object]:
    return {
        "canonical_name": candidate.canonical_name or "",
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "aliases": list(candidate.aliases),
    }


def _score_candidate_name(raw_name: str, candidate: PlayerCandidate) -> float:
    """Score *raw_name* against a provider-supplied PlayerCandidate.

    Adapts the PlayerCandidate to the dict shape expected by _score_name().
    """
    return _score_name(raw_name, _candidate_player_dict(candidate))


def _score_candidate_with_context(
    context: IdentityContext,
    candidate: PlayerCandidate,
) -> float:
    """Score a provider candidate using name evidence plus parser context signals."""
    player = _candidate_player_dict(candidate)
    score = _score_context_name(context.raw_name, player, context)

    # Context refines a plausible name match; it must never promote a
    # different-name candidate into a match by itself.
    if score < 0.80:
        return score

    if context.jersey_number and candidate.jersey_number:
        if context.jersey_number.strip() == candidate.jersey_number.strip():
            score += 0.12
        else:
            score -= 0.08

    if context.position and candidate.position:
        if context.position == candidate.position:
            score += 0.04
        else:
            score -= 0.04

    if context.team_id and candidate.team_id:
        score += 0.06 if context.team_id == candidate.team_id else -0.08

    if context.season_id and candidate.season_id:
        score += 0.04 if context.season_id == candidate.season_id else -0.06

    return max(0.0, min(1.0, score))


def _split_display_name(raw_name: str) -> tuple[str | None, str | None]:
    """Return (first_name, last_name) as unmodified display strings for DB storage."""
    raw_name = raw_name.strip()
    if not raw_name:
        return None, None
    if "," in raw_name:
        parts = [p.strip() for p in raw_name.split(",", 1)]
        return parts[1] or None, parts[0] or None
    tokens = raw_name.split()
    if len(tokens) == 1:
        return None, tokens[0]
    return " ".join(tokens[:-1]), tokens[-1]


def _preferred_canonical_name(
    context: IdentityContext,
    fallback: str | None = None,
) -> str:
    if context.canonical_name_hint and _looks_like_full_name(context.canonical_name_hint):
        return context.canonical_name_hint
    if context.first_name and context.last_name:
        full_name = f"{context.first_name} {context.last_name}".strip()
        if _looks_like_full_name(full_name):
            return full_name
    if fallback and _looks_like_full_name(fallback):
        return fallback
    return context.raw_name


def _coerce_arc_player_id(value: object) -> int | str:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value  # type: ignore[return-value]


def _looks_like_full_name(value: str | None) -> bool:
    if value is None:
        return False
    cleaned = value.strip()
    if not cleaned or "." in cleaned:
        return False
    parts = cleaned.split()
    return len(parts) >= 2 and all(not _is_initial_like(part) for part in parts)


def _is_initial_like(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    return bool(re.fullmatch(r"[A-Za-zÄÖÜÕŠŽäöüõšž]\.?,?", stripped))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_alias_sql(value: object) -> str:
    if value is None:
        return ""
    return normalize_alias(str(value))
