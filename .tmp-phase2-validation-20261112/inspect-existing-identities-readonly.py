import json, sqlite3
from pathlib import Path
p=Path('data/arc2.db').resolve()
c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
c.execute('PRAGMA query_only=ON')
c.row_factory=sqlite3.Row
print('database_path='+str(p))
for table in ('source_livestats_games','games','player_games','events','active_game_source_snapshots','source_player_occurrences','identity_decisions','player_aliases','player_external_ids'):
    exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
    print('table',table,'exists',bool(exists))
    if exists and table in ('games','source_livestats_games','player_games','source_player_occurrences','identity_decisions'):
        print('columns',table,[r['name'] for r in c.execute(f'PRAGMA table_info({table})')])
players=c.execute("SELECT player_id,canonical_name,first_name,last_name FROM players WHERE player_id IN (125,216) ORDER BY player_id").fetchall()
print('lippasaar_players',[dict(r) for r in players])
print('lippasaar_aliases',[dict(r) for r in c.execute("SELECT player_id,alias_name,source FROM player_aliases WHERE player_id IN (125,216) AND alias_name LIKE '%Lippasaar%' ORDER BY player_id,alias_name")])
print('lippasaar_external_ids',[dict(r) for r in c.execute("SELECT player_id,provider,external_player_id FROM player_external_ids WHERE player_id IN (125,216) ORDER BY player_id,provider")])
print('lippasaar_player_games',[dict(r) for r in c.execute("SELECT pg.game_id,pg.player_id,pg.player_name,pg.team_id,pg.team_name,pg.shirt_number,g.game_date,g.season_id,s.match_id,s.json_url FROM player_games pg JOIN games g USING(game_id) LEFT JOIN source_livestats_games s USING(game_id) WHERE pg.player_id IN (125,216) ORDER BY g.game_date,pg.game_id,pg.player_id")])
print('lippasaar_active_occurrences')
if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_identity_decision_sets'").fetchone():
    q="""SELECT a.game_id,a.normalization_id,o.player_occurrence_id,o.arc_player_id,p.original_name,p.first_name,p.last_name,p.jersey_number,p.provider_player_id,p.pno,tm.source_team_key,s.match_id,s.json_url FROM active_identity_decision_sets a JOIN identity_decisions o ON o.decision_set_id=a.decision_set_id JOIN source_player_occurrences p USING(player_occurrence_id) JOIN source_team_occurrences tm USING(team_occurrence_id,normalization_id) JOIN games g USING(game_id) LEFT JOIN source_livestats_games s USING(game_id) WHERE o.arc_player_id IN (125,216) ORDER BY a.game_id,p.player_occurrence_id"""
    print([dict(r) for r in c.execute(q)])
c.close()
