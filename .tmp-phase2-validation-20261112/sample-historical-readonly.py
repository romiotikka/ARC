import sqlite3
from pathlib import Path
c=sqlite3.connect(Path('data/arc2.db').resolve().as_uri()+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');c.row_factory=sqlite3.Row
q='''SELECT s.match_id,g.game_id,g.season_id,g.home_team_id,th.canonical_name home_team,g.away_team_id,ta.canonical_name away_team,g.home_score,g.away_score,(SELECT COUNT(*) FROM player_games pg WHERE pg.game_id=g.game_id) roster_rows,(SELECT COUNT(*) FROM events e WHERE e.game_id=g.game_id) event_rows,(SELECT COUNT(*) FROM player_games pg WHERE pg.game_id=g.game_id AND pg.player_id IS NULL) null_players,(SELECT COUNT(*) FROM (SELECT pg.player_name,pg.team_name,pg.shirt_number FROM player_games pg WHERE pg.game_id=g.game_id GROUP BY pg.player_name,pg.team_name,pg.shirt_number HAVING COUNT(*)>1) d) duplicate_roster_keys FROM source_livestats_games s JOIN games g USING(game_id) LEFT JOIN teams th ON th.team_id=g.home_team_id LEFT JOIN teams ta ON ta.team_id=g.away_team_id WHERE s.season_id=20222023 AND (SELECT COUNT(*) FROM events e WHERE e.game_id=g.game_id)>100 ORDER BY CAST(s.match_id AS INTEGER)'''
rows=[dict(r) for r in c.execute(q)]
print('complete_games',len(rows),'teams',len(set([r['home_team'] for r in rows]+[r['away_team'] for r in rows])))
for r in rows[::max(1,len(rows)//25)]: print(r)
print('lippasaar', [r for r in rows if r['match_id']=='2155618'])
c.close()
