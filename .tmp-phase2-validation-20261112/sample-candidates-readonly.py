import csv,sqlite3
from pathlib import Path
csv_rows=list(csv.DictReader(open('data/estlatbl/estlatbl_2026_games.csv',encoding='utf-8')))
ids=[r['match_id'] for r in csv_rows]
c=sqlite3.connect(Path('data/arc2.db').resolve().as_uri()+'?mode=ro',uri=True);c.execute('PRAGMA query_only=ON');c.row_factory=sqlite3.Row
q='''SELECT s.game_id,s.match_id,s.season_id,g.game_date,g.home_team_id,th.canonical_name home_team,g.away_team_id,ta.canonical_name away_team,g.home_score,g.away_score,g.status,(SELECT COUNT(*) FROM player_games pg WHERE pg.game_id=g.game_id) roster_rows,(SELECT COUNT(*) FROM events e WHERE e.game_id=g.game_id) event_rows,(SELECT COUNT(DISTINCT player_id) FROM player_games pg WHERE pg.game_id=g.game_id) distinct_players,(SELECT COUNT(*) FROM player_games pg WHERE pg.game_id=g.game_id AND pg.player_id IS NULL) null_players FROM source_livestats_games s JOIN games g USING(game_id) LEFT JOIN teams th ON th.team_id=g.home_team_id LEFT JOIN teams ta ON ta.team_id=g.away_team_id WHERE s.match_id IN (%s) ORDER BY s.season_id,s.match_id'''%','.join('?'*len(ids))
rows=[dict(r) for r in c.execute(q,ids)]
print('csv_count',len(ids),'existing_matches',len(rows))
print('sampled_existing')
for i in ([0,10,25,40,55,70,85,100,115,130,145,160,175,190,199]):
 if i<len(rows):print(rows[i])
print('available_nonempty',sum(1 for r in rows if r['roster_rows'] and r['event_rows']))
print('odd_rosters',[(r['match_id'],r['home_team'],r['away_team'],r['roster_rows'],r['event_rows']) for r in rows if r['roster_rows'] and (r['roster_rows']<18 or r['roster_rows']>28)][:30])
c.close()
