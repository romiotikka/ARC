import os, sqlite3
os.chdir(r'c:\Users\kasutaja\OneDrive\Töölaud\ARC')
path = r'data\\arc2.db'
con = sqlite3.connect(path)
cur = con.cursor()
print('DB_EXISTS', os.path.exists(path))
print('TABLES', [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()])
print('NONNUM COUNT', cur.execute("SELECT COUNT(*) FROM players WHERE player_id NOT GLOB '[0-9]*'").fetchone()[0])
print('NUM COUNT', cur.execute("SELECT COUNT(*) FROM players WHERE player_id GLOB '[0-9]*'").fetchone()[0])
print('DUP CANONICAL', cur.execute("SELECT canonical_name, COUNT(*) FROM players GROUP BY canonical_name HAVING COUNT(*)>1 ORDER BY COUNT(*) DESC LIMIT 50").fetchall())
print('DUP ALIAS_NAME', cur.execute("SELECT alias_name, COUNT(DISTINCT player_id) FROM player_aliases GROUP BY alias_name HAVING COUNT(DISTINCT player_id)>1 ORDER BY COUNT(DISTINCT player_id) DESC LIMIT 50").fetchall())
print('TEAM ALIASES', cur.execute("SELECT alias_name, COUNT(DISTINCT team_id) FROM team_aliases GROUP BY alias_name HAVING COUNT(DISTINCT team_id)>1 ORDER BY COUNT(DISTINCT team_id) DESC LIMIT 50").fetchall())
print('TEAMS LIST', cur.execute('SELECT team_id, canonical_name FROM teams ORDER BY team_id').fetchall())
print('ALIAS SAMPLE TOP TEAMS', cur.execute('SELECT team_id, alias_name FROM team_aliases ORDER BY alias_name LIMIT 100').fetchall())
print('PLAYER NAME SAMPLE FOR DUPLICATE CANONICAL', cur.execute("SELECT canonical_name, player_id FROM players WHERE canonical_name IN (SELECT canonical_name FROM players GROUP BY canonical_name HAVING COUNT(*)>1) ORDER BY canonical_name, player_id LIMIT 200").fetchall())
print('PLAYER_GAMES TOP 100', cur.execute('SELECT player_id, player_name, team_id, team_name, shirt_number FROM player_games ORDER BY player_name LIMIT 100').fetchall())
print('TEAM NAMES IN PLAYER_GAMES', cur.execute('SELECT DISTINCT team_name, team_id FROM player_games ORDER BY team_name LIMIT 200').fetchall())
print('TEAM DUPS BY ID SAME NAME', cur.execute('SELECT team_id, COUNT(DISTINCT team_name), GROUP_CONCAT(DISTINCT team_name) FROM player_games GROUP BY team_id HAVING COUNT(DISTINCT team_name)>1 LIMIT 200').fetchall())
print('SOURCE_GAMES COUNT', cur.execute('SELECT COUNT(*) FROM source_livestats_games').fetchone()[0])
print('SOURCE_GAMES SAMPLE', cur.execute('SELECT game_id, provider_game_id, match_id, json_url FROM source_livestats_games LIMIT 20').fetchall())
print('JSON_IN_WORKSPACE')
for root, dirs, files in os.walk('data'):
    for f in files:
        if f.lower().endswith('.json'):
            print(os.path.join(root, f))
