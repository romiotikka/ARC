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

Primary provider:

API-Sports

Purpose:

- identity verification
- season rosters
- player metadata
- external player IDs

Secondary providers:

League-specific providers

Example:

Basket.ee

Purpose:

- improve domestic player matching
- improve Estonian league coverage
- provide additional metadata
- validate local rosters

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