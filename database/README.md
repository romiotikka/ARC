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

Player registry creation is intentionally separate. For now, `player_games`
stores player names from LiveStats while `player_id` remains nullable until the
identity system is built with proper matching and review.
