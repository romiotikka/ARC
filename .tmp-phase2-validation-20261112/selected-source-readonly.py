import sqlite3
from pathlib import Path
matches=['2155561','2155618','2155633','2155677','2155924','2156193']
c=sqlite3.connect(Path('data/arc2.db').resolve().as_uri()+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');c.row_factory=sqlite3.Row
for m in matches:
 r=c.execute('''SELECT s.game_id,s.match_id,s.json_url,g.season_id,g.game_date,g.home_team_id,h.canonical_name home,g.away_team_id,a.canonical_name away,(SELECT COUNT(*) FROM player_games p WHERE p.game_id=g.game_id) players,(SELECT COUNT(*) FROM events e WHERE e.game_id=g.game_id) events FROM source_livestats_games s JOIN games g USING(game_id) LEFT JOIN teams h ON h.team_id=g.home_team_id LEFT JOIN teams a ON a.team_id=g.away_team_id WHERE s.match_id=?''',(m,)).fetchone()
 print(dict(r) if r else None)
 if r:
  print('player ids',c.execute('SELECT COUNT(DISTINCT player_id),MIN(player_id),MAX(player_id) FROM player_games WHERE game_id=?',(r['game_id'],)).fetchone())
  if m=='2155618': print('lipp', [dict(x) for x in c.execute('SELECT pg.player_id,pg.player_name,pg.shirt_number,p.canonical_name FROM player_games pg JOIN players p USING(player_id) WHERE game_id=? AND player_id IN(125,216)',(r['game_id'],))])
c.close()
