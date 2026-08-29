LOCKED ARCHITECTURE — DO NOT REINTERPRET
# ARC — Architecture & Data Specification

## 0. Document status

This document defines the locked architecture and data principles of ARC.

These decisions have already been made.
AI agents must implement them, not reinterpret or redesign them.

If the existing code conflicts with this document, the code is considered
legacy/incorrect and must be adapted to the specification.

Agents must NOT invent alternative architectures without explicit approval.

---

# 1. Current ARC goal

The immediate goal is NOT to build the final analytics product.

The immediate goal is:

> Build a clean, reliable, provider-independent ARC database and ingestion
> system from the existing legacy system.

The old database and parts of the old ingestion/identity implementation
contain known bugs and inconsistent assumptions.

We are rebuilding the data foundation rather than preserving legacy
implementation decisions merely because they already exist.

The new database must eventually allow:

- multiple leagues
- multiple seasons
- multiple data providers
- persistent player identities
- persistent team identities
- provider-specific external IDs
- reliable game/player/event/statistic data
- later analytics on top of clean underlying data

---

# 2. Core architectural principle

ARC is the canonical data layer.

Providers are sources of evidence/data.

Providers are NOT ARC.

A provider must never define ARC's internal identity system.

Conceptually:

    Provider A ─┐
    Provider B ─┤
    Provider C ─┼──> ARC identity/data layer
    Provider D ─┤
    Provider E ─┘

The same ARC entity may have identifiers in multiple providers.

---

# 3. Internal ARC IDs

ARC internal entity IDs are independent of providers.

## Players

`player_id`:

- INTEGER
- internally assigned by ARC
- starts from 1
- increases for newly created players
- never derived from name
- never derived from provider ID
- never a UUID
- never changed because a player changes teams

One `player_id` represents one real person.

## Teams

`team_id`:

- INTEGER
- internally assigned by ARC
- starts from 1
- increases for newly created teams
- never derived from team name
- never a provider ID

A team name is data, not an identifier.

## Leagues

`league_id` is an ARC internal identifier.

Provider/source keys are separate.

Example:

    ARC league_id = 1
    source/provider key = "estlatbl"

"estlatbl" must never become the ARC integer league_id.

---

# 4. Player identity

The fundamental rule:

> One ARC player_id = one real person.

The resolver must prevent:

- duplicate ARC players for the same person
- merging two different people into one player
- provider-specific identities becoming ARC identities

Players can move between teams.

Therefore ARC must NOT model a player as a permanent
`player + team + season` identity.

A player changing teams must normally retain the same ARC player_id.

A new-team occurrence must first check whether the player already exists.

---

# 5. Canonical name

`canonical_name` is the ARC representation of the person.

It is the name that ARC should display to users.

It is NOT automatically the raw provider display name.

Example:

    Provider:
    "K. Kitsing"

does not mean:

    canonical_name = "K. Kitsing"

if a reliable full name is available elsewhere.

A canonical name should preferably be the best known full human-readable
name.

Temporary/incomplete canonical names are allowed when the available
evidence genuinely does not provide a better name.

ARC must NEVER invent a full name.

Provider name forms may be stored as source names / aliases / evidence.

---

# 6. Names and aliases

Different representations of the same person are expected.

Examples:

    Kristjan Kitsing
    K. Kitsing
    Kitsing, Kristjan
    Kristjan KITSING

Name normalization is used for matching.

Normalization must NOT destroy the identity information.

Do NOT use lossy "remove letters" logic as the identity solution.

Diacritics, punctuation, ordering and formatting differences may be
handled for matching, while preserving the original source representation.

Aliases are evidence supporting an identity.

Do not create artificial aliases simply because a single source name was
observed once.

---

# 7. Identity resolution hierarchy

Identity resolution uses all available evidence.

General priority:

1. Established ARC local identity
2. Strong provider external-ID evidence
3. Name evidence
4. Contextual evidence
5. Other applicable providers
6. Manual review when ambiguity remains

Local ARC information should be preferred when it is sufficiently strong.

However:

> If there is meaningful uncertainty, use applicable external providers
> to help establish the correct identity.

Context can include:

- team
- league
- season
- jersey number
- position
- historical games
- previously established provider mappings
- other identity evidence

Context supports a plausible name match.

Context must NOT turn an obviously conflicting person into a match.

---

# 8. Providers

ARC must support multiple independent providers.

Examples may include:

- LiveStats
- Basket.ee
- API-Sports
- future providers

No provider is globally authoritative.

Basket.ee is a source for the leagues/data it covers.
It is NOT "ARC's second universal source".

API-Sports is another provider.

LiveStats is another provider.

The system must allow different providers to have different external IDs
for the same ARC entity.

---

# 9. External IDs

External IDs belong to providers, not ARC.

Conceptually:

    ARC player_id = 17

    provider        external_id
    --------------------------------
    livestats       12345
    basket.ee       67890
    api-sports      99999

All may refer to the same ARC player.

The same applies to teams and leagues where applicable.

If a provider does not supply an external ID, that is valid.

ARC must NOT invent an external ID.

The absence of an external ID must not prevent identity resolution when
other evidence is sufficient.

---

# 10. LiveStats

LiveStats is a data source, not the definition of ARC identity.

LiveStats game/event/statistic data should be preserved accurately.

If the LiveStats payload contains a usable player/team external ID:

    preserve it
    pass it through the import pipeline
    associate it with the provider mapping

If the payload does not contain a usable external ID:

    do not invent one.

Use name + context + other provider evidence when necessary.

---

# 11. Data source combination

The purpose of multiple providers is partly identity resolution.

Example:

    LiveStats:
        "K. Kitsing"
        team = X
        season = Y

    Basket.ee:
        "Kristjan Kitsing"
        basket player ID = ABC

    API-Sports:
        "Kristjan Kitsing"
        API player ID = XYZ

ARC may determine:

    ARC player_id = 17
    canonical_name = "Kristjan Kitsing"

with provider mappings:

    LiveStats → ...
    Basket.ee → ABC
    API-Sports → XYZ

The provider data is evidence that is combined into one ARC identity.

---

# 12. Uncertain cases

The system must be conservative.

If an existing identity is clearly established:

    USE existing ARC player.

If the match is plausible but uncertain:

    CHECK applicable external providers.

If evidence remains ambiguous:

    REVIEW.

Do NOT create a duplicate player merely because the provider uses a
different name representation.

Do NOT force a merge merely because the player is on the same team.

CREATE is the last safe option when no plausible existing identity exists
and available evidence supports a new person.

---

# 13. Teams

Teams follow the same fundamental separation:

    ARC team_id
    canonical team name
    provider external IDs
    source names / aliases

Team names must never be used as ARC primary IDs.

Team identity must consider provider IDs and contextual evidence where
available.

Simple "same name = same team" logic is not sufficient as a universal
identity rule.

---

# 14. Existing game/stat/event architecture

The identity redesign must NOT unnecessarily rewrite:

- LiveStats event semantics
- statistic parsing
- PlayerGame statistics
- event parsing
- lineup reconstruction
- lineup validation

The existing functionality should be preserved unless a change is
strictly necessary to make the new identity/data model work correctly.

Identity is a foundation underneath these systems.

---

# 15. Lineups

Lineup problems previously observed were substantially caused by incorrect
player identity/ID handling.

The lineup system must ultimately operate on stable ARC player IDs.

Do NOT solve identity problems by adding arbitrary lineup-specific identity
logic.

The identity layer must provide reliable ARC player IDs to downstream
systems.

---

# 16. Database rebuild

The current legacy database is NOT considered authoritative.

The target is a clean database generated from the current schema and
correct ingestion logic.

A clean rebuild must be possible.

Initial validation should use a small number of real games before importing
large historical datasets.

---

# 17. Current implementation status

The current system has already implemented significant parts of:

- provider-specific league mappings
- identity resolver
- LiveStats importer
- event/statistic ingestion
- player identity evidence
- provider mappings
- clean database schema

However, legacy code remains in parts of the system.

Current work is therefore:

> Identify and remove legacy assumptions that conflict with this
> specification while preserving already-correct functionality.

Do NOT redesign ARC from scratch.

---

# 18. Current MVP data target

The first MVP will initially focus on Estonian-Latvian Basketball League
data.

The exact number of seasons is not yet locked.

The important objective is reliable underlying data first.

Analytics is a major part of the eventual ARC product and will be built
ON TOP OF the clean data foundation.

---

# 19. Agent rules

Before modifying code, an AI agent MUST:

1. Read this document.
2. Inspect the existing implementation.
3. Identify exactly where the implementation conflicts with this document.
4. Make the smallest coherent changes necessary.
5. Preserve unrelated working functionality.
6. Never silently change an architectural decision.
7. Never introduce a parallel identity architecture.
8. Never invent missing provider data.
9. Never fabricate external IDs.
10. Never automatically merge ambiguous people.
11. Never use provider IDs as ARC IDs.
12. Never use names as ARC primary IDs.
13. Never treat a provider as globally authoritative.

If implementation and this specification conflict:

    specification = intended behavior
    existing code = implementation to be corrected

If the specification is genuinely ambiguous:

    stop and report the ambiguity rather than inventing a new rule.

---

# 20. Validation requirement

A change is not considered complete merely because unit tests pass.

The system must eventually pass a clean real-data test:

    fresh database
        ↓
    provider/game mappings
        ↓
    real LiveStats game
        ↓
    identity resolution
        ↓
    teams
        ↓
    players
        ↓
    PlayerGames
        ↓
    events
        ↓
    lineups

The resulting database must have valid foreign keys and stable ARC IDs.

Re-importing the same game must not create duplicate ARC identities.

---

# 21. DO NOT DO

Never:

- replace ARC IDs with UUIDs
- derive IDs from names
- use team names as team IDs
- treat LiveStats short names as canonical names automatically
- invent provider IDs
- make Basket.ee the universal identity authority
- make API-Sports the universal identity authority
- blindly trust any provider
- use lossy character deletion as identity matching
- create duplicates when identity is uncertain
- rewrite lineup/statistics systems unnecessarily
- redesign the architecture without explicit approval