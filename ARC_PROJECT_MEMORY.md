# ARC Project Memory

This file captures the strategic and architectural context provided by the user.
Use it as the default decision anchor for future ARC work.

## Product Vision

ARC is a basketball intelligence and decision-support platform.

ARC is not meant to become:

- a generic statistics website
- another player database
- an AI report generator

ARC should help clubs answer practical basketball decisions:

- Which player should we recruit?
- Which league is this player's performance likely to translate to?
- Which player fits our roster, role, and style best?
- How much of a player's production comes from context versus ability?
- How should we construct the roster?

Long-term focus areas:

- recruitment intelligence
- player evaluation
- player similarity
- player fit analysis
- roster construction
- league translation models
- team analysis
- lineup impact analysis

## Core Architecture Principle

ARC should be built around basketball entities, not external provider data
structures. Providers can change; ARC's internal model should stay stable.

Preferred architecture:

```text
Data Collection
  -> Database
  -> Analytics Engine
  -> Query Engine / API
  -> User Interface
  -> AI Interpretation Layer
```

Another valid user-facing flow:

```text
User
  -> AI Interpreter
  -> ARC Analytics Engine
  -> ARC Database
```

AI is not the analytics engine and not the primary source of basketball
intelligence. AI should mainly:

- interpret user intent
- translate requests into database queries or analytics requests
- explain and format results
- improve usability

The primary value should come from:

- database quality
- analytics models
- translation models
- decision-support workflows
- user interface

## Development Philosophy

Do not spend development time repeatedly confirming facts already known with
high confidence.

Prioritize:

- new capabilities
- analytics logic
- architecture decisions
- information generation

Avoid unnecessary rediscovery or repeated validation of already-understood
LiveStats structures.

## Current Data Source

Primary source:

- FIBA LiveStats

Confirmed available:

- game metadata
- team boxscore data
- player boxscore data
- play-by-play events
- shot chart coordinates
- starters
- substitutions
- plus/minus
- actionNumber linkage between shot data and play-by-play events

Confirmed workflow:

```text
League website -> Game page -> LiveStats matchId -> data.json
```

For EstLatBL:

```text
game_id -> matchId -> data.json
```

The major LiveStats reverse-engineering phase is considered largely complete.

## Key LiveStats Discoveries

Starter information exists directly in LiveStats. It does not need to be
reconstructed.

Substitution events exist directly in play-by-play, for example:

```text
Player A OUT
Player B IN
```

Because starters and substitutions are available, full lineup reconstruction
should eventually be possible.

Shot coordinates are available. Useful fields include:

- x
- y
- player
- actionType
- result
- period

Shot data and play-by-play events are linked by:

```text
actionNumber
```

This allows shots to be linked to their corresponding events.

## Core Entity Model

Current agreed core entities:

- League
- Season
- Team
- Player
- Game
- TeamGame
- PlayerGame
- Event

Conceptual relationship:

```text
Game
  -> TeamGames
  -> PlayerGames
  -> Events
```

No additional core entities are currently planned.

## Entity Philosophy

### League

Persistent basketball competition.

Primary identifier:

```text
league_id
```

### Season

Season is a global system-wide dimension, not a child object of League.

Example:

```text
season_id = 20262027
```

This represents the 2026-27 season across ARC.

Examples that should all reference the same `season_id`:

- Estonian-Latvian League 2026-27
- Korisliiga 2026-27
- LEB Oro 2026-27

League and Season are independent dimensions.

### Team

Team represents the basketball organization, not a sponsor-name snapshot.

Sponsor name changes should not create new team IDs.

Example:

```text
TalTech
TalTech/ALEXELA
```

should generally remain the same `team_id`.

### Player

Player is a persistent identity across seasons, teams, leagues, and countries.

A player keeps the same `player_id` throughout his career.

### Game

Game should contain factual game information only, such as:

- date
- venue
- attendance
- participating teams
- final score

Game should not store derived context as primary fields.

Derived context includes:

- rest days
- travel distance
- back-to-back status
- schedule density
- games in last 7 days

These should be generated later through context-generation or analytics layers.

### TeamGame

TeamGame should be a first-class stored object.

Reason: many future analytics operate primarily at team-game level.

Examples:

- pace
- offensive profile
- defensive profile
- rebounding profile
- shot profile
- team comparison

### PlayerGame

PlayerGame is the first ARC-native object already built as Parser v1.

Purpose:

```text
LiveStats JSON -> Parser -> ARC PlayerGame object
```

PlayerGame is not a LiveStats object. It converts provider data into ARC's
standardized structure.

Current PlayerGame fields include:

- game_id
- player_name
- team_name
- shirt_number
- position
- minutes
- points
- rebounds
- assists
- steals
- blocks
- turnovers
- shooting statistics
- plus_minus
- starter

Advanced metrics are intentionally excluded from PlayerGame parsing. They
should be calculated later by ARC.

### Event

Event is the lowest-level truth source.

Examples of Event types:

- shot
- rebound
- turnover
- foul
- substitution
- free throw
- other basketball actions

Important principle:

```text
Shot is an Event type, not a top-level entity.
```

Long-term goal: PlayerGame and TeamGame should theoretically be reconstructable
from Events.

## Fact Layer vs Analytics Layer

Stored fact layer:

- League
- Season
- Team
- Player
- Game
- TeamGame
- PlayerGame
- Event

Calculated analytics layer:

- PlayerSeason
- TeamSeason
- possessions
- lineup segments
- advanced metrics
- similarity scores
- fit scores
- league translation outputs

The fact layer represents basketball facts. The analytics layer represents
interpretation of facts.

## Identity System

Persistent identifiers are fundamental:

- league_id
- season_id
- team_id
- player_id
- game_id

Names should never be treated as primary identifiers.

Current practical player matching approach:

- full name
- team
- league
- jersey number

Preferred long-term identity architecture:

```text
Player Registry
  <- Official Team Rosters
  <- Game Data
```

The registry should support:

- player tracking across seasons
- player tracking across teams
- player tracking across leagues

Current matching should be semi-automated where useful:

```text
96% confidence -> proposed player_id = X -> optional human review
```

Official roster information should eventually become the primary identity
source.

Identity quality is extremely important.

## Possession Philosophy

Possession is not currently a core database object.

Possessions should be reconstructed from Events and belong to the analytics
layer.

Expected flow:

```text
Events -> Possession Reconstruction -> Possession Objects
```

No current plan to create persistent IDs for possessions.

## Lineup Philosophy

Lineups are strategically important but are not a core registry object.

Future pipeline:

```text
Events
  -> Lineup Segments
  -> On/Off Analysis
  -> Impact Models
  -> Fit Models
```

Inputs:

- starters
- substitutions

Outputs:

- active lineup by time segment
- lineup combinations
- lineup plus/minus
- on/off impact

Lineup reconstruction is a high-priority future analytics-engine component.

## Analytics Direction

ARC is transitioning from data acquisition and discovery toward database
architecture and analytics-engine development.

Potential analytics layers include:

- lineup analysis
- on/off impact
- plus/minus models
- player impact models
- player similarity
- player fit
- recruitment models
- league translation models
- roster construction tools

Future discussions should prioritize:

- scalable database design
- registry architecture
- identity management
- analytics infrastructure
- recruitment modeling
- lineup analytics
- league translation systems

over additional reverse engineering of already-understood LiveStats structures.

## League Translation Engine

One of ARC's most strategically important future ideas.

Goal:

Model how player performance translates between leagues.

Examples:

- NCAA -> Finland
- NCAA -> Sweden
- NCAA -> Estonia
- NCAA -> Poland
- Sweden -> Estonia
- Estonia -> Poland

This is not the same as player career tracking. The goal is statistical
translation and transfer evaluation.

## Player Fit Engine

Core question:

```text
Not: How good is this player?
But: How well does this player fit this team, role, roster, and style of play?
```

Potential outputs:

- fit scores
- similarity scores
- recruitment recommendations
- roster construction support

## Recruitment Intelligence

ARC should move beyond:

```text
Player Search
```

toward:

```text
Player Recommendation
Recruitment Decision Support
```

## Competitive Landscape

Closest discovered competitor:

- Basketball Scout AI

Observed capabilities:

- 553+ leagues
- NCAA coverage
- 100k+ players
- player search
- scouting reports
- AI reports
- player comparison
- player journeys / career tracking

Strategic conclusion:

Market demand is validated.

The question is no longer:

```text
Does anybody need this?
```

The question is:

```text
What can ARC do better?
```

Large databases alone are not a sustainable competitive advantage.

ARC should not position itself as:

```text
Another player database
```

ARC should position itself as:

```text
European Basketball Intelligence Platform
```

## Differentiators

Potential ARC differentiators:

- League Translation Engine
- Player Fit Engine
- Lineup Impact Analytics
- Recruitment Intelligence
- Small and mid-sized European league expertise
- Deep FIBA LiveStats-derived analytics
- Roster construction support

ARC should focus on creating new information and decision-support value.

## Target Leagues

Core target region:

- Northern Europe

Current priority leagues:

- Estonia
- Latvia
- Finland
- Sweden
- Denmark
- Iceland

Additional important future targets:

- Poland
- Belgium
- Italy 1st division
- Italy 2nd division
- Spain 1st division
- Spain 2nd division
- NCAA

NCAA is strategically important because it is one of the largest sources of
players entering European basketball.

## Current Project Stage

ARC is no longer primarily a data-discovery project.

LiveStats reverse engineering is largely complete.

ARC is entering:

- database architecture
- registry design
- data acquisition strategy
- analytics-engine development

The next major challenge is no longer parsing individual games.

The next major challenge is building:

- player registry
- team registry
- game registry
- league registry
- scalable multi-league database

These foundations should later support:

- translation models
- recruitment models
- fit models
- lineup models

## Current Core Belief

ARC should become a recruitment and basketball intelligence platform that helps
clubs make better player acquisition and roster construction decisions using
structured basketball data, league translation models, and decision-support
analytics.

