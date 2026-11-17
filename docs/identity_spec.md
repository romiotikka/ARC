# ARC Identity System Specification (Identity v2)

Version: 1.0
Status: Approved Architecture
Author: ARC Project

---

# 1. Purpose

The purpose of the Identity System is to ensure that every real basketball player is represented by exactly one permanent ARC `player_id`.

Identity resolution must be independent of:

- LiveStats naming format
- league
- season
- team
- provider

The resolver is responsible for identifying players consistently across all supported competitions.

---

# 2. Core Principles

## One player = One ARC player_id

An ARC player_id represents one real human player.

It must never represent:

- multiple people
- temporary LiveStats names
- provider-specific identities

Once created, an ARC player_id is permanent.

---

## Resolver owns identity

The Identity Resolver is the only component allowed to:

- create players
- merge aliases
- assign provider IDs
- update player identity

No parser may create or modify player identities directly.

---

## Parser responsibilities

The parser only imports basketball data.

The parser provides:

- LiveStats player name
- team_id
- season_id
- league_id
- jersey number (if available)

The parser then requests:

player_id = resolver.resolve(context)

The parser never decides player identity.

---

# 3. Identity Flow

LiveStats

↓

IdentityContext

↓

ARC Database

↓

API-Sports Provider

↓

Local Provider (Basket.ee etc.)

↓

Manual Review

↓

player_id

---

# 4. Resolver Algorithm

Step 1

Attempt to resolve the player using existing ARC identities.

Inputs:

- aliases
- canonical names
- previous confirmations
- team
- season
- jersey number

If exactly one confident match exists:

RETURN player_id

---

Step 2

Request the current roster from API-Sports.

The resolver searches only inside the returned roster.

Global player searches should be avoided whenever possible.

API-Sports is the primary identity provider.

---

Step 3

If API-Sports cannot confidently resolve the player:

Use a local provider.

Examples:

- Basket.ee
- federation databases
- league-specific providers

Local providers are especially important for:

- EstLat
- KML
- other domestic competitions

---

Step 4

If the player matches an existing ARC player:

Update the existing identity.

Never create a duplicate player.

---

Step 5

If no ARC player matches:

Estimate confidence that the player is genuinely new.

If confidence >= 85%

Create new ARC player.

If confidence < 85%

Send to Manual Review.

---

# 5. Confidence Principles

Confidence should consider:

- last name
- first name / initial
- aliases
- jersey number
- team
- season
- provider roster
- birth date
- height
- nationality

No single attribute should determine identity.

---

# 6. Player Updates

Resolver may improve existing player information after identity has been confirmed.

Examples:

K. Kitsing

↓

Kristjan Kitsing

NULL height

↓

204 cm

NULL birth date

↓

1990-12-15

Resolver must never overwrite confirmed data with lower-quality information.

---

# 7. Provider Chain

Provider order is competition-specific. For EstLatBL, Basket.ee is the primary
roster source and API-Sports is the fallback. Other competitions retain their
configured provider order; API-Sports may remain primary where appropriate.

Purpose:

- identity verification
- season rosters
- player metadata
- external player IDs

League-specific providers:

League-specific providers

Example:

Basket.ee

Purpose:

- improve domestic player matching
- improve Estonian league coverage
- provide additional metadata
- validate local rosters

---

# 7A. API-Sports Runtime Mappings and Coverage

Persistent API-Sports mappings and observed coverage are stored in
`scripts/identity/providers/api_sports_mappings.json`. The `leagues` section maps
ARC league IDs to API-Sports league IDs. The `seasons` section maps ARC season
IDs to API-Sports season codes. The `teams` section is scoped by ARC league ID
and maps the LiveStats team code to the API-Sports team ID. LiveStats codes are
used because ARC team IDs are created during game import and are not stable
configuration keys.

The LiveStats parser includes the game league and season, the created ARC team
ID, and the source team code in each resolver occurrence. Before resolving
players, `resolve_livestats_players.py` loads the mapping file and persists the
matching API-Sports external IDs in the existing league, season, and team
mapping tables. The resolver requests a roster only when all three mappings are
available. `ARC_API_SPORTS_MAPPINGS` can override the file for a deployment or
an isolated test; setting it to an empty string disables the file mapping.

Mappings do not imply data coverage. The separate `coverage` section records
observed endpoint results and API-reported coverage. As checked on 2026-11-10
for Latvia-Estonian League 204:

- API season 2022: team lookup returned 16 teams and VEF roster returned 18
	players, including Roberts Berzins (7177), although API metadata reported
	player coverage as false.
- API season 2023: team lookup returned 16 teams and VEF roster returned 17
	players, including Roberts Berzins (7177).
- API season 2024: team lookup returned 15 teams and VEF roster returned 17
	players; Roberts Berzins was not returned.
- API season 2025: league metadata recognizes the season, but both team and
	direct VEF roster requests were rejected by the current free plan.

The current cache stores roster outcomes by provider, external league, season,
and team under the operating-system user cache directory (overridable with
`ARC_IDENTITY_ROSTER_CACHE_DIR`). Successful rosters expire after seven days,
empty rosters and unsupported-coverage responses after one day, and other
provider errors after five minutes. These distinct outcomes are logged and are
not treated as equivalent to missing mappings. Missing league, season, or team
mappings are logged before that provider is skipped. Basket.ee remains a
separate provider; because its team IDs are season-specific, its roster IDs are
loaded from the season-scoped config described below rather than written to the
seasonless team mapping table. EuroBasket is not implemented.

To add a season, verify it through the API-Sports league endpoint, then test
team retrieval and representative team-season roster requests. Add its ARC to
API season code mapping and record actual coverage separately. To add a team,
match its LiveStats code and ARC team to the name returned by the mapped league
team endpoint; do not infer IDs from names alone. Update the scoped `teams`
mapping only after that crosswalk is verified.

For match 2155133, LiveStats supplies `Roberts Berzins` as the full name for
`R. Berzins`. The API-Sports VEF Riga 2022 roster returns Roberts Berzins
(7177). The cache and mapping path expose this candidate to the normal resolver
scoring and review flow. The provider evidence does not authorize a match based
only on a surname or initial, and existing conflicting ARC evidence can still
require review.

# 7B. Basket.ee First for EstLatBL

Basket.ee roster mappings and provider order are stored in
`scripts/identity/providers/basket_mappings.json`. ARC season IDs map to Basket
`setSid` values; each season maps LiveStats team codes to the team IDs verified
on that season's participating-teams page. Some team-season mappings are
intentionally absent where the team name or identity could not be verified.

For ARC league 1, the resolver checks Basket.ee first. It reads complete names,
jersey numbers, and positions from roster pages, and complete profile fields
from player pages. The roster-page jersey and position take precedence over
missing profile fields. A season/team-scoped roster mapping is passed in memory
to the resolver because Basket team IDs change by season and the existing ARC
external-team-ID table has no season key. This does not write mappings or
identities during configuration loading.

Within the selected team-season roster, full names are compared in either
display order. Jersey numbers can separate similar initial forms. A full-name
candidate whose jersey conflicts with another plausible roster member's jersey
requires REVIEW. Provider-based automatic matching requires a matching jersey
in the mapped team-season roster when name evidence is based on initials or is
otherwise non-strong. A unique strong full-name match may resolve without a
jersey. A conflicting jersey remains REVIEW, and a number alone never
establishes identity. Weak, malformed local structured-name candidates do not block a
strong provider match unless they have verified identity status or matching
team-season-player-game evidence. Multiple strong local or provider candidates
remain REVIEW. If Basket has no usable roster candidate or mapping, API-Sports
is tried next. A strong Basket candidate is not replaced by weaker API-Sports
name evidence. Conflicting provider candidates remain REVIEW.

To add a Basket season, verify its `setSid` and team IDs from that season's
participating-teams page, then add only confirmed LiveStats-code/team-ID pairs
to `basket_mappings.json`. The adapter caches the roster by provider, league,
season, and team. Unmapped teams are logged and can fall through to API-Sports;
Basket.ee remains separate from API-Sports.

---

# 8. Provider Trust

Provider trust is field-specific.

Example:

Height

Prefer local provider if available.

Birth date

Prefer API-Sports.

Roster

Prefer provider responsible for that competition.

Canonical name

Prefer the highest-quality available source.

---

# 9. Player Model

Each player represents one real person.

Preferred fields:

player_id

first_name

last_name

canonical_name

birth_date

height_cm

nationality

position

created_at

updated_at

Missing values should remain NULL.

Resolver should improve them over time.

---

# 10. Player Aliases

Aliases exist only for identity resolution.

Examples:

Kristjan Kitsing

K. Kitsing

Kristjan Andre Kitsing

Kitsing, Kristjan

Šmits

Smits

Aliases improve matching.

They are not the primary display name.

---

# 11. External Provider IDs

Every confirmed provider identity should be stored.

Example:

player_id

provider

external_player_id

Once linked, future lookups should reuse the external ID instead of repeating name searches.

---

# 12. Team Changes

The resolver must never assume that a player remains with one team during a season.

If the same confirmed player appears on another team's roster:

Do NOT create a new player.

Instead:

Confirm identity.

Associate the existing player with the new team through future game imports.

---

# 13. Position Normalization

Internal ARC positions:

G

F

C

Conversions:

PG → G

SG → G

SF → F

PF → F

C → C

Numeric positions:

1 → G

2 → G

3 → F

4 → F

5 → C

If multiple groups are observed:

G + F

↓

G-F

F + C

↓

F-C

Resolver may expand player position over time.

---

# 14. Manual Review

Manual Review should be used whenever confidence is insufficient.

Examples:

Common names

Missing metadata

Conflicting provider information

Uncertain transfers

Manual Review is preferred over incorrect automatic identity assignment.

---

# 15. Future Expansion

The resolver should support additional providers without architecture changes.

Future providers may include:

- Eurobasket
- FIBA
- NCAA
- federation databases

Every provider should produce the same normalized identity objects.

Resolver logic should remain provider-independent.

---

# Guiding Principle

Creating an unnecessary new player is preferable to incorrectly merging two different people.

Incorrect merges permanently damage identity integrity.

False positives are therefore considered more serious than false negatives.