import os
import shutil
import sqlite3
from collections import Counter, defaultdict

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(root, "data", "arc2.db")
backup_path = os.path.join(root, "data", "arc2.db.bak")

if not os.path.exists(db_path):
    raise FileNotFoundError(f"Database not found: {db_path}")

if not os.path.exists(backup_path):
    shutil.copy2(db_path, backup_path)
    print(f"Created backup: {backup_path}")
else:
    print(f"Backup already exists: {backup_path}")

conn = sqlite3.connect(db_path)
conn.execute("PRAGMA foreign_keys = OFF")
cur = conn.cursor()

players = {
    row[0]: {
        "canonical_name": row[1],
        "birth_date": row[2],
        "nationality": row[3],
        "height_cm": row[4],
        "created_at": row[5],
    }
    for row in cur.execute(
        "SELECT player_id, canonical_name, birth_date, nationality, height_cm, created_at FROM players"
    )
}
player_aliases = [
    row for row in cur.execute("SELECT alias_id, player_id, alias_name, source, created_at FROM player_aliases")
]

# Map alias-only IDs to existing canonical players when alias names match.
canonical_by_name = defaultdict(list)
for pid, data in players.items():
    canonical_by_name[data["canonical_name"]].append(pid)

alias_id_map = {}
for alias_id, pid, alias_name, source, created_at in player_aliases:
    if pid not in players and alias_name in canonical_by_name:
        alias_id_map[pid] = canonical_by_name[alias_name][0]

updated_player_aliases = []
for alias_id, pid, alias_name, source, created_at in player_aliases:
    mapped_pid = alias_id_map.get(pid, pid)
    updated_player_aliases.append((alias_id, mapped_pid, alias_name, source, created_at))

# Build connected components based on exact canonical name or shared alias.
by_name = defaultdict(list)
for pid, data in players.items():
    by_name[data["canonical_name"]].append(pid)

by_alias = defaultdict(list)
for alias_id, pid, alias_name, source, created_at in updated_player_aliases:
    by_alias[alias_name].append(pid)

adj = defaultdict(set)
for group in list(by_name.values()) + list(by_alias.values()):
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            adj[group[i]].add(group[j])
            adj[group[j]].add(group[i])

all_ids = sorted(players.keys())
visited = set()
components = []
for pid in all_ids:
    if pid in visited:
        continue
    stack = [pid]
    comp = []
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        comp.append(current)
        for neighbor in adj[current]:
            if neighbor not in visited:
                stack.append(neighbor)
    components.append(sorted(comp))

print(f"Found {len(components)} distinct player identity groups from {len(all_ids)} player rows")

# Assign new numeric IDs sequentially.
new_id_map = {}
for index, comp in enumerate(sorted(components, key=lambda x: x[0]), start=1):
    new_id = str(index)
    for pid in comp:
        new_id_map[pid] = new_id

# Build merged player rows.
new_players = {}
for comp in components:
    new_id = new_id_map[comp[0]]
    canonical_names = [players[pid]["canonical_name"] for pid in comp]
    canonical_name = Counter(canonical_names).most_common(1)[0][0]

    birth_dates = [players[pid]["birth_date"] for pid in comp if players[pid]["birth_date"]]
    birth_date = birth_dates[0] if birth_dates else None

    nationalities = [players[pid]["nationality"] for pid in comp if players[pid]["nationality"]]
    nationality = nationalities[0] if nationalities else None

    heights = [players[pid]["height_cm"] for pid in comp if players[pid]["height_cm"] is not None]
    height_cm = heights[0] if heights else None

    created_at_values = [players[pid]["created_at"] for pid in comp if players[pid]["created_at"]]
    created_at = min(created_at_values) if created_at_values else None

    new_players[new_id] = {
        "canonical_name": canonical_name,
        "birth_date": birth_date,
        "nationality": nationality,
        "height_cm": height_cm,
        "created_at": created_at,
    }

# Build merged aliases.
new_aliases = defaultdict(set)
new_alias_sources = defaultdict(lambda: defaultdict(Counter))
new_alias_created = defaultdict(lambda: defaultdict(list))
for alias_id, pid, alias_name, source, created_at in updated_player_aliases:
    if pid not in new_id_map:
        continue
    mapped = new_id_map[pid]
    new_aliases[mapped].add(alias_name)
    new_alias_sources[mapped][alias_name][source] += 1
    if created_at:
        new_alias_created[mapped][alias_name].append(created_at)

# Create temporary tables and populate them.
cur.execute("DROP TABLE IF EXISTS players_new")
cur.execute(
    '''
    CREATE TABLE players_new (
        player_id TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        birth_date TEXT,
        nationality TEXT,
        height_cm INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    ''',
)
cur.execute("DROP TABLE IF EXISTS player_aliases_new")
cur.execute(
    '''
    CREATE TABLE player_aliases_new (
        alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id TEXT NOT NULL,
        alias_name TEXT NOT NULL,
        source TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(player_id),
        UNIQUE (player_id, alias_name)
    )
    ''',
)

for new_id, row in new_players.items():
    cur.execute(
        '''
        INSERT INTO players_new (player_id, canonical_name, birth_date, nationality, height_cm, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (new_id, row["canonical_name"], row["birth_date"], row["nationality"], row["height_cm"], row["created_at"]),
    )

for new_id, alias_names in new_aliases.items():
    for alias_name in sorted(alias_names):
        source = new_alias_sources[new_id][alias_name].most_common(1)[0][0]
        created_at = min(new_alias_created[new_id][alias_name]) if new_alias_created[new_id][alias_name] else None
        if created_at:
            cur.execute(
                '''
                INSERT INTO player_aliases_new (player_id, alias_name, source, created_at)
                VALUES (?, ?, ?, ?)
                ''',
                (new_id, alias_name, source, created_at),
            )
        else:
            cur.execute(
                '''
                INSERT INTO player_aliases_new (player_id, alias_name, source)
                VALUES (?, ?, ?)
                ''',
                (new_id, alias_name, source),
            )

# Update foreign key references to player_id in player_games and events.
cur.execute('CREATE TEMP TABLE player_id_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL)')
for old_id, new_id in new_id_map.items():
    cur.execute('INSERT INTO player_id_map (old_id, new_id) VALUES (?, ?)', (old_id, new_id))
cur.execute(
    '''
    UPDATE player_games
    SET player_id = (
        SELECT new_id FROM player_id_map WHERE old_id = player_games.player_id
    )
    WHERE player_id IS NOT NULL
    ''',
)
cur.execute(
    '''
    UPDATE events
    SET player_id = (
        SELECT new_id FROM player_id_map WHERE old_id = events.player_id
    )
    WHERE player_id IS NOT NULL
    ''',
)

# Replace old player tables with cleaned ones.
cur.execute('ALTER TABLE players RENAME TO players_old')
cur.execute('ALTER TABLE player_aliases RENAME TO player_aliases_old')
cur.execute('ALTER TABLE players_new RENAME TO players')
cur.execute('ALTER TABLE player_aliases_new RENAME TO player_aliases')

# Rebuild team_aliases by ensuring all player_games team names are present.
existing_team_aliases = {
    (row[0], row[1])
    for row in cur.execute('SELECT team_id, alias_name FROM team_aliases').fetchall()
}
for team_id, alias_name in cur.execute('SELECT DISTINCT team_id, team_name FROM player_games').fetchall():
    if team_id is None or alias_name is None:
        continue
    if (team_id, alias_name) not in existing_team_aliases:
        cur.execute(
            '''
            INSERT INTO team_aliases (team_id, alias_name, source)
            VALUES (?, ?, ?)
            ''',
            (team_id, alias_name, 'fiba_livestats'),
        )

conn.commit()
conn.close()
print('Cleanup completed. Numeric player IDs assigned and player duplicates merged.')
print('New distinct player count:', len(new_players))
