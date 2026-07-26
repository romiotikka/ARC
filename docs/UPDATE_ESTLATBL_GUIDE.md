# EstLat Automatic Update Pipeline - Implementation Guide

## Overview

A production-ready, long-term maintenance solution for automatic EstLat data collection and import.

The pipeline is **idempotent** — running it multiple times produces no duplicate data.

## What Was Changed

### 1. Collection Script: `scripts/import/collect_estlatbl_seasons.mjs`
**Changes**: Added CLI parameter support for incremental collection

- `--recent` — collect only the most recent 2 seasons (default: all 4)
- `--recent-count N` — customize how many seasons (e.g., `--recent-count 3`)

**Why**: Reduces unnecessary API calls to estlatbl.com. Most updates are for active/upcoming seasons.

**Backward compatible**: Script works unchanged when called without flags.

### 2. Import Script: `scripts/import/import_livestats_games.mjs`
**Changes**: Minimal enhancement to track failures in Dead Letter Queue

- Added import of `DeadLetterQueue` utility
- Error categorization (network_error, json_parse_error, data_validation_error)
- DLQ recording on import failure (non-blocking)
- Better error reporting and stats

**Existing behavior preserved**:
- Automatic duplicate prevention (query filters already-imported games)
- Automatic player/team creation
- Transaction-based safety (ROLLBACK on failure)
- Continues processing on individual game failures

**Why**: Failures are now tracked for analysis and retry, not just logged.

### 3. New: Orchestrator Script `scripts/import/update_estlatbl.mjs`
**Purpose**: Coordinates entire update pipeline

**Responsibilities**:
- Initialize database (apply migrations)
- Run collection script with appropriate flags
- Run import script with appropriate limits
- Track execution time and provide reporting
- Log all activity to file

**Configuration**: CLI flags for different scenarios
- Default: `node scripts/import/update_estlatbl.mjs`
- Collect all seasons: `node scripts/import/update_estlatbl.mjs --all-seasons`
- Import all pending: `node scripts/import/update_estlatbl.mjs --import-all`
- Custom: `node scripts/import/update_estlatbl.mjs --recent-count 3 --import-limit 10`

**Exit codes**:
- `0` — Success
- `1` — Collection/import script failed
- `2` — Database initialization failed

### 4. New: Utility Module `scripts/lib/estlatbl-utils.mjs`
**Components**:

**Logger class** — File-based logging
- Logs to `logs/estlatbl-update.log`
- Timestamps each entry
- Simultaneous console output for monitoring

**DeadLetterQueue class** — Failed import tracking
- Records failures with error type and message
- Tracks retry count
- Provides stats on pending failures
- Non-blocking on import failure

**Database utilities**
- `initializeDatabase()` — Applies migrations on startup
- `getDatabasePath()` — Consistent path handling

### 5. New: Database Migration `database/migrations/001_add_failed_imports_dlq.sql`
**Creates**: `failed_imports` table for Dead Letter Queue

| Column | Purpose |
|--------|---------|
| `game_id` | Identifies the failed game |
| `match_id` | Provider reference |
| `error_type` | Category: network_error, json_parse_error, data_validation_error, database_error |
| `error_message` | Details for debugging |
| `failed_at` | When first failure occurred |
| `retry_count` | Number of failed attempts |
| `status` | pending, scheduled_retry, permanent_failure |

**Indexes** on `status`, `failed_at`, `retry_count` for efficient querying.

---

## Duplicate Prevention Mechanism

### Collection (estlatbl.com scraping)
- Uses UPSERT statements (`INSERT ... ON CONFLICT DO UPDATE`)
- Re-running collection on same season overwrites previous mappings
- No duplicate game_id entries

### Import (FIBA LiveStats)
- Query filters for games **without** `team_games` records
  ```sql
  SELECT s.* FROM source_livestats_games s
  LEFT JOIN team_games tg ON tg.game_id = s.game_id
  WHERE tg.team_game_id IS NULL  -- Only unimported games
  ```
- Imported games never imported again
- Verified safe for incremental updates

---

## Manual Execution

### Default (Recommended for daily automation)
```bash
cd C:\path\to\ARC
node scripts\import\update_estlatbl.mjs
```

**Behavior**:
- Collects last 2 seasons from estlatbl.com
- Imports up to 5 new games from FIBA LiveStats
- Duration: ~2-5 minutes (network-dependent)
- Safe to run multiple times per day

**Output**:
```
╔═══════════════════════════════════════════════════════════╗
║    EstLat Automatic Update Pipeline                       ║
╚═══════════════════════════════════════════════════════════╝
Started at 2026-06-24T00:00:00.000Z
Initializing database...
✓ Database initialized successfully
Step 1: Collecting game mappings from EstLat...
2025-26: game_ids=8, mappings=8, missing_match_id=0
2024-25: game_ids=12, mappings=12, missing_match_id=0
✓ Collection complete
Step 2: Importing game boxscores from FIBA LiveStats...
Imported estlatbl_12345 (98765): Team A vs Team B, player_games=24
Imported estlatbl_12346 (98766): Team C vs Team D, player_games=22
...
✓ Import complete

=== Update Complete ===
Duration: 127s
Configuration: recent_seasons=true, import_limit=5
```

### Full Refresh (Initial setup or rebuild)
```bash
node scripts\import\update_estlatbl.mjs --all-seasons --import-all
```

**Behavior**:
- Collects all 4 seasons
- Imports all pending games (no limit)
- Full historical refresh
- Use sparingly (30+ minutes, heavy API load)

### Custom Configuration
```bash
# Collect 3 most recent seasons, import up to 10 games
node scripts\import\update_estlatbl.mjs --recent-count 3 --import-limit 10

# Collect all seasons, import only 3 games
node scripts\import\update_estlatbl.mjs --all-seasons --import-limit 3

# Collect recent, import all pending (no limit)
node scripts\import\update_estlatbl.mjs --import-all
```

---

## Windows Task Scheduler Configuration

### Setup Steps

1. **Open Task Scheduler**
   - Press `Win + R`, type `taskschd.msc`, press Enter
   - Or: Control Panel → Administrative Tools → Task Scheduler

2. **Create New Task**
   - Right-click "Task Scheduler Library" → "Create Basic Task..."
   - Name: `EstLat Daily Update`
   - Description: `Automatic collection and import of EstLat basketball data`
   - Click "Next >"

3. **Set Schedule**
   - Trigger: "Daily"
   - Start: `2026-06-25` (tomorrow) or your preferred date
   - Time: `00:00` (midnight)
   - Recur every: `1 day`
   - Click "Next >"

4. **Set Action**
   - Action: "Start a program"
   - Program/script: `node`
   - Add arguments: `C:\path\to\ARC\scripts\import\update_estlatbl.mjs`
   - Start in: `C:\path\to\ARC`
   - Click "Next >"

5. **Configure Options** (Recommended)
   - ✓ Run whether user is logged in or not
   - ✓ Run with highest privileges (if applicable)
   - ✓ If the running task does not end when requested, force it to stop
   - Set "If the task is already running, then the following rule applies": **Do not start a new instance**
   - Click "Next >"

6. **Finish**
   - Verify settings and click "Finish"

### Verify Setup

```powershell
# List all EstLat tasks
Get-ScheduledTask | Where-Object {$_.TaskName -like "*EstLat*"}

# Test run (right-click task → Run in Task Scheduler, or PowerShell):
Start-ScheduledTask -TaskName "EstLat Daily Update"

# Check recent runs
Get-ScheduledTaskInfo "EstLat Daily Update"
```

### Check Logs After First Run

```bash
# View update log (most recent first)
Get-Content C:\path\to\ARC\logs\estlatbl-update.log -Tail 50

# Or use tail for live monitoring
tail -f C:\path\to\ARC\logs\estlatbl-update.log
```

---

## Monitoring & Troubleshooting

### Check Update Logs

```bash
# Last 50 lines of log
tail -50 logs/estlatbl-update.log

# Find errors only
grep "ERROR\|✗" logs/estlatbl-update.log

# View log from specific time
grep "2026-06-24" logs/estlatbl-update.log
```

### Check Dead Letter Queue

```bash
# From database CLI (sqlite3)
sqlite3 data/arc2.db

> SELECT COUNT(*) FROM failed_imports WHERE status='pending';
> SELECT game_id, match_id, error_type, failed_at, retry_count 
  FROM failed_imports WHERE status='pending' ORDER BY failed_at DESC;

> .quit
```

### Common Issues

| Issue | Symptom | Solution |
|-------|---------|----------|
| **Network timeout** | "LiveStats request failed: 408" in log | Check internet connection; increase import-limit to reduce batch size |
| **No games imported** | "No LiveStats games to import" | Collection may have found no new games; run collection manually to verify |
| **Database locked** | "database is locked" error | Ensure only one update process runs at a time; check Task Scheduler "Do not start new instance" setting |
| **Node not found** | "node: command not found" | Verify node.js PATH in Task Scheduler action; use full path like `C:\Program Files\nodejs\node.exe` |
| **Permission denied** | Cannot write to logs/ | Check file permissions on `logs/` directory |

### Retry Failed Games

Failed games are recorded in `failed_imports` table with `status='pending'`.

To retry:
```bash
# Manual re-run of import will retry pending games
# Failed games stay in queue and are retried next update
node scripts/import/update_estlatbl.mjs --import-all
```

---

## Data Quality Guarantees

✓ **No duplicate imports** — Query-based filtering prevents re-import  
✓ **Incremental by design** — Only missing games are imported  
✓ **Player auto-creation** — New players created with normalized names  
✓ **Team aliases** — All team name variations stored  
✓ **Failure isolation** — One failed game doesn't stop the process  
✓ **Idempotent** — Safe to run multiple times  
✓ **Status tracking** — Game status updated (scheduled_or_import_pending → boxscore_imported)

---

## Performance Characteristics

| Operation | Typical Duration | API Calls | Notes |
|-----------|------------------|-----------|-------|
| Collect 2 seasons | 30-60s | ~20-30 (estlatbl.com) | Rate-limited by sleeps |
| Collect 4 seasons | 60-120s | ~40-60 (estlatbl.com) | Full historical refresh |
| Import 5 games | 15-30s | 5-10 (FIBA LiveStats) | Per-game, one at a time |
| Import all pending | 5-30 min | Variable | Depends on queue size |
| Full pipeline | ~2-5 min | ~30 total | Default config, typical run |

---

## Architecture

```
Windows Task Scheduler
    ↓ (daily at 00:00)
update_estlatbl.mjs (orchestrator)
    ├─ Initialize database (migrations)
    ├─ collect_estlatbl_seasons.mjs --recent
    │   └─ estlatbl.com scraper
    │   └─ Upsert to source_livestats_games
    ├─ import_livestats_games.mjs --limit 5
    │   ├─ Query unimported games
    │   └─ FIBA LiveStats JSON parser
    │   └─ Auto-create players/teams
    │   └─ Write to player_games, team_games
    │   └─ DLQ tracking on errors
    └─ Report stats to logs/estlatbl-update.log
```

---

## Next Steps (Optional Future Extensions)

- **Retry logic**: Automatic retry of `retry_count < 3` failures
- **Backoff strategy**: Exponential backoff on repeated failures
- **Slack notifications**: Alert on pipeline failure or DLQ threshold
- **Health checks**: Pre-pipeline validation of API connectivity
- **Incremental timestamps**: Track `last_collected_at` per season for smarter incremental
- **Seasonal flags**: Auto-detect active seasons dynamically
- **Resilience**: Circuit breaker pattern for API failures

---

## Safety Notes

- **No data loss**: All operations use UPSERT/INSERT...ON CONFLICT
- **Transaction safety**: Individual games wrapped in transactions
- **Failure recovery**: Failed games recorded in DLQ, not lost
- **Idempotency**: Designed to tolerate duplicate executions
- **No blocking failures**: One game failure doesn't stop the pipeline

**Risk**: If FIBA LiveStats API returns corrupted data, it will be imported. Manual review and correction needed. DLQ tracks these for investigation.

