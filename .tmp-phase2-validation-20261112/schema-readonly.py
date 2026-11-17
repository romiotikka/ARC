import sqlite3
from pathlib import Path
c=sqlite3.connect(Path('data/arc2.db').resolve().as_uri()+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON')
for t in ['seasons','teams','team_aliases','players','player_aliases','player_external_ids','team_external_ids','league_external_ids','season_external_ids','league_games','source_livestats_games','player_games']:
 try: print(t,[r[1] for r in c.execute(f'pragma table_info({t})')])
 except sqlite3.Error as e: print(t,e)
for t in ['player_aliases','team_aliases']:
 try: print('sample',t,c.execute(f'SELECT * FROM {t} WHERE '+('player_id=125' if t=='player_aliases' else 'team_id=8')+' LIMIT 5').fetchall())
 except sqlite3.Error as e: print(e)
c.close()
