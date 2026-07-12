# EstLat Pipeline - Quick Reference

## One-Time Setup
```bash
cd C:\path\to\ARC
node scripts\setup_estlatbl.mjs
```

## Manual Execution

### Daily Update (Recommended)
```bash
node scripts\update_estlatbl.mjs
```
- Collects last 2 seasons
- Imports up to 5 games
- Duration: ~2-5 minutes

### Full Refresh
```bash
node scripts\update_estlatbl.mjs --all-seasons --import-all
```
- Collects all seasons
- Imports all pending games
- Duration: 30+ minutes

### Custom Configuration
```bash
# Collect 3 seasons, import 10 games
node scripts\update_estlatbl.mjs --recent-count 3 --import-limit 10

# Collect all, import 20 games
node scripts\update_estlatbl.mjs --all-seasons --import-limit 20

# Collect recent, import all
node scripts\update_estlatbl.mjs --import-all
```

## Windows Task Scheduler Setup (PowerShell - Run as Admin)

```powershell
$taskName = "EstLat Daily Update"
$scriptPath = "C:\path\to\ARC\scripts\update_estlatbl.mjs"
$workingDir = "C:\path\to\ARC"

$trigger = New-ScheduledTaskTrigger -Daily -At 00:00
$action = New-ScheduledTaskAction -Execute "node" `
  -Argument $scriptPath -WorkingDirectory $workingDir
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName `
  -Trigger $trigger -Action $action -Settings $settings -RunLevel Highest -Force

# Test
Start-ScheduledTask -TaskName $taskName

# Verify
Get-ScheduledTaskInfo -TaskName $taskName
```

## Monitoring

### View Latest Log (Last 50 lines)
```bash
tail -50 logs/estlatbl-update.log
```

### Find Errors
```bash
grep "ERROR\|✗" logs/estlatbl-update.log
```

### Check Failed Imports (DLQ)
```bash
sqlite3 data/arc.db
> SELECT COUNT(*) as pending FROM failed_imports WHERE status='pending';
> SELECT game_id, error_type, retry_count FROM failed_imports 
  WHERE status='pending' ORDER BY failed_at DESC LIMIT 5;
> .quit
```

## Troubleshooting

### Task Not Running
```powershell
# Check if task exists
Get-ScheduledTask -TaskName "EstLat Daily Update"

# Get last run info
Get-ScheduledTaskInfo -TaskName "EstLat Daily Update"

# Check event log for errors
Get-EventLog -LogName "System" -Source "TaskScheduler" | 
  Where-Object {$_.Message -like "*EstLat*"}
```

### Database Locked
- Check no other process is running
- Verify Task Scheduler setting: "Do not start new instance"
- Restart Task Scheduler if needed: `net stop schedule` and `net start schedule`

### Node Not Found
- Use full path in Task Scheduler: `C:\Program Files\nodejs\node.exe`
- Verify Node.js installed: `node --version`

## Key Files

| File | Purpose |
|------|---------|
| `scripts/update_estlatbl.mjs` | Main orchestrator |
| `scripts/collect_estlatbl_seasons.mjs` | Collection (modified) |
| `scripts/import_livestats_games.mjs` | Import (modified) |
| `scripts/lib/estlatbl-utils.mjs` | Utilities (Logger, DLQ) |
| `logs/estlatbl-update.log` | Update log |
| `data/arc.db` | Database with DLQ table |
| `UPDATE_ESTLATBL_GUIDE.md` | Comprehensive guide |
| `IMPLEMENTATION_REPORT.md` | Full technical report |

## Exit Codes

- `0` — Success
- `1` — Pipeline error (check log)
- `2` — Setup/database error (check log)

## Performance

| Config | Duration | API Calls |
|--------|----------|-----------|
| Default (2 seasons, 5 games) | 2-5 min | ~25 |
| Full refresh | 30+ min | ~100+ |
| Collection only | 1-2 min | ~20 |
| Import only | 1-5 min | 5-20 |

## Database Schema (New)

```sql
failed_imports (
  failed_import_id INTEGER PRIMARY KEY,
  game_id TEXT NOT NULL,
  match_id TEXT NOT NULL,
  error_type TEXT,          -- network_error, json_parse_error, etc.
  error_message TEXT,
  failed_at TEXT,
  last_error_at TEXT,
  retry_count INTEGER,
  status TEXT,              -- pending, scheduled_retry, permanent_failure
  UNIQUE (game_id, match_id)
)
```

## Key Features

✓ Incremental updates (recent seasons only by default)  
✓ Duplicate prevention (UPSERT + query filters)  
✓ Automatic player/team creation  
✓ Dead Letter Queue for failed imports  
✓ File-based logging  
✓ Idempotent (safe to run multiple times)  
✓ Continues on individual game failures  
✓ Windows Task Scheduler compatible  

## Support & Next Steps

1. **Read**: `UPDATE_ESTLATBL_GUIDE.md` (comprehensive)
2. **Reference**: `IMPLEMENTATION_REPORT.md` (technical details)
3. **Deploy**: Follow setup checklist in IMPLEMENTATION_REPORT.md
4. **Monitor**: Check logs regularly for first week
5. **Optimize**: Adjust `--recent-count` and `--import-limit` based on game frequency

---

**Last Updated**: 2026-06-24  
**Status**: ✓ Production Ready
