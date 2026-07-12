ARC SYSTEM SPECIFICATION
STATUS

This document contains accepted ARC architecture decisions.

These decisions are requirements.

Do not redesign them.

Do not propose alternative architectures.

Do not propose additional core entities.

Do not generate architecture reviews.

Do not generate strategy reports.

Do not revisit accepted decisions.

If an implementation problem is discovered:

explain the problem
explain the impact
propose the smallest possible change

Default assumption:

ARC architecture is frozen.

Continue implementation.

PROJECT PURPOSE

ARC is a basketball analytics and decision-support platform.

Primary goals:

player evaluation
player recruitment
player similarity
player fit analysis
roster construction
league translation models
team analysis

ARC is not:

primarily an AI product
primarily a reporting platform
primarily a player database
primarily a statistics viewer

Statistics are inputs.

Decision support is the output.

ARCHITECTURE

Data Collection

↓

Database

↓

Analytics Engine

↓

Query Engine

↓

User Interface

↓

AI Interpretation Layer

AI is not the analytics engine.

AI responsibilities:

convert user intent into database queries
convert user intent into analytics requests
explain outputs
improve usability

Primary value comes from:

database quality
analytics models
translation models
DEVELOPMENT PHILOSOPHY

Priorities:

Database architecture
Data acquisition
Identity resolution
Database growth
Analytics foundations
Advanced analytics
AI features

Avoid:

unnecessary testing
repeated validation of already known facts
architecture redesign
speculative future entities
theoretical improvements that do not immediately help implementation

Prefer:

implementation
database construction
scalable data collection
reusable ingestion systems

over:

discussions
reports
architecture reviews
CORE ENTITY MODEL

The following entities are accepted and frozen.

League

Season

Team

Player

Game

TeamGame

PlayerGame

Event

Do not introduce additional core entities.

PERSISTENT IDENTIFIERS

The following identifiers are accepted and frozen.

league_id

season_id

team_id

player_id

game_id

These are ARC-owned identifiers.

Do not introduce additional persistent identifiers without strong justification.

Objects that currently do NOT require persistent identifiers:

possessions
lineup segments
advanced metrics
fit scores
similarity scores
translation outputs

These belong to analytics layers.

LEAGUE

League represents a basketball competition.

Examples:

EstLatBL
KML
LBL
Korisliiga
ACB
Serie A

League is an independent registry entity.

League metadata may include:

name
country
tier

Country and tier are factual metadata.

SEASON

Season is a global dimension.

Example:

season_id = 20262027

represents:

2026-27

across the entire ARC system.

Examples:

EstLatBL 2026-27
KML 2026-27
LBL 2026-27
Korisliiga 2026-27

all reference the same season_id.

Season is NOT subordinate to League.

League and Season are independent dimensions.

Games link them together.

TEAM

team_id represents a basketball organization.

Sponsor changes do not create a new team.

Examples:

TalTech

TalTech/ALEXELA

TTÜ

must resolve to the same team_id.

Names are aliases.

team_id is the identity.

The objective is to track basketball organizations, not sponsor names.

PLAYER

player_id represents a real human player.

player_id remains stable across:

teams
leagues
seasons

A player keeps the same player_id throughout their career.

Provider IDs may be stored.

Provider IDs are external references.

Provider IDs are not primary identities.

PLAYER REGISTRY

Player Registry is a core ARC concept.

Long-term philosophy:

Player Registry

↑

Official Rosters

↑

Game Data

Identity should not depend entirely on game data.

Official rosters are expected to become an important identity source.

Reasons:

duplicate names exist
spelling variations exist
transliteration differences exist
middle names exist

Examples:

Jānis Bērziņš

Janis Berzins

may represent the same player.

Different players may also share identical names.

PLAYER MATCHING

Current preferred approach:

Semi-automated matching.

Before creating a new player_id:

attempt matching using:

full name
team
league
jersey number
roster information when available

If a reasonable match exists:

reuse existing player_id.

If no reasonable match exists:

create a new player_id.

If ambiguity exists:

flag for review.

Do not delay implementation waiting for a perfect matching system.

GAME

Game contains factual game information.

Examples:

date
teams
venue
attendance
score

Game should not contain derived context.

Examples of derived context:

travel distance
rest days
opponent strength
schedule density
games played in previous time windows

These are calculated later.

COMPETITION TYPE

Game should contain factual competition context.

Examples:

Regular Season
Playoffs
Cup
Friendly
Qualification

These are facts.

Store them.

Do not create derived competition strength ratings at this stage.

TEAMGAME

TeamGame is a first-class stored entity.

Examples of future usage:

pace
offensive profile
defensive profile
rebounding profile
shot profile

Many future analytics queries will operate directly on TeamGame.

Store TeamGame.

PLAYERGAME

PlayerGame is a first-class stored entity.

Many future analytics queries will operate directly on PlayerGame.

Store PlayerGame.

PlayerGame is expected to become one of the most frequently queried entities in ARC.

EVENT

Event is the lowest-level truth source.

Hierarchy:

Game

└── Event

Examples of event types:

Shot
Rebound
Turnover
Foul
Substitution
Free Throw

Shot is not a top-level entity.

Shot is a subtype of Event.

Long-term objective:

PlayerGame and TeamGame should theoretically be reconstructable from Events.

FACT LAYER

Stored:

League

Season

Team

Player

Game

TeamGame

PlayerGame

Event

These represent basketball facts.

ANALYTICS LAYER

Calculated later.

Examples:

PlayerSeason
TeamSeason
Possessions
Lineups
Advanced Metrics
Similarity Scores
Fit Scores
Translation Outputs

These represent interpretations of facts.

Do not move analytical objects into the fact layer.

LINEUPS

Lineups are important.

Expected future flow:

Events

↓

Lineup Segments

↓

On/Off Analysis

↓

Impact Models

↓

Fit Models

Lineups belong to the analytics layer.

Not the registry layer.

POSSESSIONS

Possessions are reconstructed.

Expected flow:

Game

↓

Events

↓

Possessions

Possessions belong to the analytics layer.

Not the registry layer.

LIVESTATS

Primary source:

FIBA LiveStats

Confirmed available:

game data
team statistics
player statistics
play-by-play
shot chart coordinates
starters
substitutions

Important discovery:

actionNumber

links play-by-play data and shot chart data.

This discovery is accepted.

Do not re-investigate it.

CURRENT LEAGUE PRIORITIES

Priority order:

EstLatBL
Estonia KML
Latvia LBL
Finland Korisliiga

Target:

minimum 5 seasons of historical data.

Additional leagues may be added later.

NCAA

NCAA is strategically important.

Reasons:

source of imported players
recruitment relevance
league translation relevance

Example future use cases:

NCAA → Finland
NCAA → Sweden
NCAA → Estonia
NCAA → Poland

The objective is not NCAA coverage itself.

The objective is better player evaluation and translation modelling.

COMPETITIVE POSITIONING

Basketball Scout AI validates market demand.

ARC does not attempt to compete through database size alone.

ARC aims to differentiate through:

league translation models
player fit models
recruitment intelligence
lineup-based analytics
European basketball specialization
CURRENT IMPLEMENTATION PRIORITIES

Current priorities:

Database schema
Identity registry
Historical data collection
League expansion

Current focus:

schema improvements
registry implementation
ingestion pipelines
historical imports

Not current focus:

AI features
report generation
front-end features
architecture redesign
AI AGENT INSTRUCTIONS

Read this file first.

Assume all decisions in this file are accepted.

Do not propose alternative architectures.

Do not generate strategy discussions.

Do not generate architecture reviews.

Do not generate long reports.

Focus only on:

implementation
schema work
ingestion
identity management
database construction
historical data collection

Architecture is frozen.

Continue implementation.