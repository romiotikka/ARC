# ARC Database

ARC currently uses SQLite for the first local database layer.

Database file:

```text
data/arc2.db
```

Schema:

```text
database/schema.sql
```

## Initialize Database

Create the database, apply schema, seed EstLatBL league/season, and import
LiveStats game mappings from `data/estlatbl/estlatbl_2026_games.csv`:

```bash
node --no-warnings scripts/import/init_database.mjs
```

## Import LiveStats Boxscores

Collect the latest four EstLatBL seasons into `games` and
`source_livestats_games`:

```bash
node --no-warnings scripts/import/collect_estlatbl_seasons.mjs
```

Import one not-yet-imported LiveStats game:

```bash
node --no-warnings scripts/import/import_livestats_games.mjs --limit 1
```

Import all not-yet-imported LiveStats games:

```bash
node --no-warnings scripts/import/import_livestats_games.mjs --all
```

Import a specific LiveStats match:

```bash
node --no-warnings scripts/import/import_livestats_games.mjs --match-id 2836380
```

## Current Fact Tables

- `leagues`
- `seasons`
- `teams`
- `team_aliases`
- `players`
- `player_aliases`
- `games`
- `team_games`
- `player_games`
- `events`
- `source_livestats_games`

## Current Import Status

The EstLatBL import stores:

- league and season reference rows
- multi-season games
- LiveStats game mappings
- team identities and aliases
- team-game rows
- player boxscore rows in `player_games`

Player occurrences are resolved through `IdentityResolver` during LiveStats
import. ARC creates persistent integer `player_id` values, reuses sufficiently
strong local identities, consults configured provider evidence when local
evidence is uncertain, and sends ambiguous/conflicting cases to review by
failing the affected import transaction and recording the failure in
`failed_imports`. A roster player is not silently persisted with a NULL
`player_id` by the production importer.

`player_games.player_id` remains nullable in the schema because the data model
must support records whose source occurrence has no resolved ARC identity and
because event/playerless source data can legitimately lack a player. Such a
NULL value must represent an explicitly handled unresolved or playerless case,
not a bypass of identity resolution. Source names and provider IDs remain in
their dedicated columns/tables.
