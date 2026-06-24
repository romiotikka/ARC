import os
import shutil
import sqlite3

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(root, "data", "arc2.db")
backup_path = os.path.join(root, "data", "arc2.db.team_fix.bak")

if not os.path.exists(db_path):
    raise FileNotFoundError(f"Database not found: {db_path}")

if not os.path.exists(backup_path):
    shutil.copy2(db_path, backup_path)
    print(f"Created team backup: {backup_path}")
else:
    print(f"Team backup already exists: {backup_path}")

conn = sqlite3.connect(db_path)
conn.execute("PRAGMA foreign_keys = OFF")
cur = conn.cursor()

teams = cur.execute('SELECT team_id, canonical_name FROM teams ORDER BY canonical_name').fetchall()
print('Existing teams:')
for row in teams:
    print(' ', row)

team_id_map = {}
for index, (old_id, canonical_name) in enumerate(teams, start=1):
    team_id_map[old_id] = str(index)

print('Computed numeric team_id map:')
for old_id, new_id in team_id_map.items():
    print(' ', old_id, '=>', new_id)

cur.execute('CREATE TEMP TABLE team_id_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL)')
for old_id, new_id in team_id_map.items():
    cur.execute('INSERT INTO team_id_map (old_id, new_id) VALUES (?, ?)', (old_id, new_id))

# Update teams table
for old_id, new_id in team_id_map.items():
    cur.execute('UPDATE teams SET team_id = ? WHERE team_id = ?', (new_id, old_id))

# Update team_aliases
cur.execute(
    '''
    UPDATE team_aliases
    SET team_id = (
      SELECT new_id FROM team_id_map WHERE old_id = team_aliases.team_id
    )
    WHERE team_id IS NOT NULL
    ''',
)

# Update team_games team_id and opponent_team_id
cur.execute(
    '''
    UPDATE team_games
    SET team_id = (
      SELECT new_id FROM team_id_map WHERE old_id = team_games.team_id
    )
    WHERE team_id IS NOT NULL
    ''',
)
cur.execute(
    '''
    UPDATE team_games
    SET opponent_team_id = (
      SELECT new_id FROM team_id_map WHERE old_id = team_games.opponent_team_id
    )
    WHERE opponent_team_id IS NOT NULL
    ''',
)

# Update games home/away team IDs
cur.execute(
    '''
    UPDATE games
    SET home_team_id = (
      SELECT new_id FROM team_id_map WHERE old_id = games.home_team_id
    )
    WHERE home_team_id IS NOT NULL
    ''',
)
cur.execute(
    '''
    UPDATE games
    SET away_team_id = (
      SELECT new_id FROM team_id_map WHERE old_id = games.away_team_id
    )
    WHERE away_team_id IS NOT NULL
    ''',
)

# Update player_games team_id
cur.execute(
    '''
    UPDATE player_games
    SET team_id = (
      SELECT new_id FROM team_id_map WHERE old_id = player_games.team_id
    )
    WHERE team_id IS NOT NULL
    ''',
)

# Update events team_id
cur.execute(
    '''
    UPDATE events
    SET team_id = (
      SELECT new_id FROM team_id_map WHERE old_id = events.team_id
    )
    WHERE team_id IS NOT NULL
    ''',
)

conn.commit()

# Verify
print('Verification:')
print('teams', cur.execute('SELECT team_id, canonical_name FROM teams ORDER BY team_id').fetchall())
print('distinct team_ids in player_games', [r[0] for r in cur.execute('SELECT DISTINCT team_id FROM player_games ORDER BY team_id').fetchall()])
print('distinct team_ids in games home', [r[0] for r in cur.execute('SELECT DISTINCT home_team_id FROM games ORDER BY home_team_id').fetchall()])
print('distinct team_ids in games away', [r[0] for r in cur.execute('SELECT DISTINCT away_team_id FROM games ORDER BY away_team_id').fetchall()])
print('distinct team_ids in team_games', [r[0] for r in cur.execute('SELECT DISTINCT team_id FROM team_games ORDER BY team_id').fetchall()])
print('distinct opponent_team_id in team_games', [r[0] for r in cur.execute('SELECT DISTINCT opponent_team_id FROM team_games ORDER BY opponent_team_id').fetchall()])
print('distinct team_ids in events', [r[0] for r in cur.execute('SELECT DISTINCT team_id FROM events ORDER BY team_id').fetchall()])

conn.close()
print('Team ID migration completed.')
