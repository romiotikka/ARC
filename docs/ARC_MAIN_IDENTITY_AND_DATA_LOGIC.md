# ARC Identity & Data Collection Logic

## Purpose

This document defines the agreed logic for how ARC collects basketball data from sources such as FIBA LiveStats and resolves source players and teams to persistent ARC entities.

This is an implementation contract. Existing identity logic should be connected to this model rather than redesigned unless explicitly decided otherwise.
## 1. Source data

ARC receives a game from a source, currently primarily FIBA LiveStats.

The LiveStats JSON contains game/event data and game context such as date, teams, league, season and location. It also contains player occurrences, names, jersey numbers, positions, play-by-play events and related information.

A LiveStats player is generally represented in a source-specific form such as `#16 M. Bernard`.

The source representation is not automatically the ARC identity.

## 2. Game context

The source game provides the context used for identity resolution.

Conceptually:

- league
- season
- game
- team
- player occurrence
- jersey number
- position

For example:

`EstLatBL | 2025/26 | Tartu Ülikool | #16 | PG | M. Bernard`

## 3. ARC persistent identities

ARC owns persistent internal identifiers:

- `player_id` — integer
- `team_id` — integer
- `league_id` — integer
- `season_id` — integer

Provider identifiers are separate from ARC identifiers.

A provider external ID must never be confused with an ARC internal ID.

## 4. Player identity resolution

Every source player occurrence must ultimately resolve to one persistent ARC player identity, or be explicitly sent to manual review.

Example:

LiveStats provides `#16 M. Bernard` for Tartu Ülikool in 2025/26.

If ARC already knows with sufficient confidence that this is:

`Malcolm Bernard, player_id = 123`

the occurrence is linked to `player_id = 123`.

The event and statistical records then belong to that persistent ARC player.

## 5. Local ARC matching comes first

ARC first uses information already known inside ARC.

Relevant evidence can include:

- raw source name;
- known name variants;
- aliases;
- canonical name;
- team context;
- season context;
- jersey number;
- position;
- other available player metadata;
- existing identity/provider mappings.

The purpose is to determine whether ARC already knows who the source occurrence represents.

A sufficiently strong local match should not be unnecessarily overridden by an external provider.

## 6. External providers are evidence sources

If ARC's own information is insufficient or ambiguous, external providers can supply additional evidence.

Examples include:

- API-Sports
- Basket.ee
- other approved providers

Providers may be queried using provider-native identifiers where appropriate and return normalized candidate information.

Providers do not make the final ARC identity decision.

Conceptually:

`Source occurrence → ARC local evidence → uncertain → external provider(s) → candidate(s) → ARC resolver + scoring → MATCH / REVIEW / CREATE`

## 7. Scoring

Scoring is part of identity resolution.

It evaluates how strongly a candidate profile matches the source occurrence and its context.

For example, ARC may compare:

Source:
- M. Bernard
- #16
- Tartu Ülikool
- 2025/26
- PG

Candidate:
- Malcolm Bernard
- #16
- Tartu Ülikool
- 2025/26
- PG

Evidence can include:

- name similarity;
- known aliases/name variants;
- team match;
- season match;
- jersey number;
- position;
- provider external-ID evidence;
- other reliable profile attributes.

The exact weights and thresholds are implementation details. The architectural purpose is to decide whether the evidence is strong enough to safely identify the player.

The system should avoid both incorrectly merging different people and creating unnecessary duplicate ARC profiles.

## 8. Provider information can improve ARC identity knowledge

Reliable new information from providers can be persisted to an ARC identity where appropriate.

Examples:

- `M. Bernard`
- `Malcolm Bernard`
- `Bernard, Malcolm`

may become known name variants/aliases of the same player.

Provider external IDs can also be stored as mappings to the ARC identity.

This creates a feedback loop:

`first occurrence → provider evidence/manual confirmation → richer ARC identity → easier future resolution`

## 9. New or uncertain players

If no reliable existing identity can be established:

### Create

If the evidence supports that this is genuinely a new player, ARC creates a new persistent integer `player_id` and stores the available identity information.

### Manual review

If plausible existing candidates exist but the evidence is insufficient to choose safely, ARC requires manual confirmation.

A wrong identity merge is more damaging than temporarily leaving an identity unresolved.

ARC must never invent provider external IDs. If a provider does not supply one, it remains unavailable.

## 10. Team identity

Teams represent persistent organizations.

A `team_id` therefore belongs to the organization, not merely to one spelling of a team name in one season.

Team names can change and different provider/source names can refer to the same organization. These names are aliases/name variants of the organization.

Provider team IDs are stored separately and can help verify or link the organization.

The system must not create a new ARC team merely because a source uses a different name for the same organization.

## 11. Event and statistics linkage

A source event may refer to `#16 M. Bernard`.

ARC must resolve that occurrence to `player_id = 123`.

The event/statistical record is then associated with that persistent ARC player identity.

The same player identity must be reusable across games and seasons.

Conceptually:

`LiveStats JSON → player occurrence → IdentityResolver → player_id → event/statistics`

## 12. Desired end-to-end pipeline

`SOURCE`
→ parse game JSON
→ obtain game context and source occurrences
→ resolve player/team identities
→ use ARC local evidence first
→ use external providers when needed
→ score candidates
→ `MATCH / REVIEW / CREATE`
→ obtain persistent ARC IDs
→ write `player_games`, events, lineups and statistics
→ ARC database

The game context is retained as part of the source/game data and is also used as identity evidence.

## 13. Architecture rule for future development

The main task is not to repeatedly redesign identity resolution.

The project contains production code as well as provider adapters, import scripts, migrations, tests, recovery tools and other supporting code. They must not automatically be treated as one production path.

Before changing code:

1. Determine whether the code is actually on the relevant production data path.
2. Understand its input and output.
3. Check it against this logic and the main ARC specification.
4. Prefer fixing an existing legacy/integration mismatch over creating a parallel implementation.
5. Do not remove old code merely because it looks old; understand its role first.
6. Do not redesign established identity logic without an explicit architectural decision.

The target is one coherent pipeline from source data to persistent ARC entities.

## 14. Current MVP direction

The immediate technical objective is a reliable clean database/import pipeline.

Preferred progression:

`clean database → one real LiveStats game → inspect identities and relationships → multiple games → larger historical import`

The data/identity foundation must be trustworthy before large-scale collection.

The long-term value of ARC is the analytics layer built on top of this reliable, persistent, cross-game data foundation.
