import sqlite3
from pathlib import Path

ROOT=Path.cwd()
SOURCE=ROOT/'data'/'arc2.db'
TARGET=Path('.tmp-phase2-extended-20261113/arc-phase2-extended.sqlite').resolve()
MATCHES=('2155561','2155618','2155633','2155677','2155924','2156193')
src=sqlite3.connect(SOURCE.resolve().as_uri()+'?mode=ro',uri=True)
src.execute('PRAGMA query_only=ON')
src.row_factory=sqlite3.Row
rows=[]
for match in MATCHES:
 row=src.execute('''SELECT s.*,g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,g.venue,g.attendance,g.competition_type
 FROM source_livestats_games s JOIN games g USING(game_id) WHERE s.match_id=?''',(match,)).fetchone()
 if row is None: raise RuntimeError(f'Missing source mapping {match}')
 if not str(row['json_url']).startswith('https://fibalivestats.dcd.shared.geniussports.com/data/'+match+'/data.json'):
  raise RuntimeError(f'Unexpected provider URL for {match}: {row["json_url"]}')
 rows.append(dict(row))
game_ids=[r['game_id'] for r in rows]
marks=','.join('?' for _ in game_ids)
player_ids=sorted({r[0] for r in src.execute(f'SELECT DISTINCT player_id FROM player_games WHERE game_id IN ({marks}) AND player_id IS NOT NULL',game_ids)})
team_ids=sorted({x for r in rows for x in (r['home_team_id'],r['away_team_id']) if x is not None})
if 125 not in player_ids or 216 not in player_ids: raise RuntimeError('Selected Lippasaar mapping does not include both ARC identities')
# Copy only seasons needed by selected games and referenced team aliases.
season_ids={int(r['season_id']) for r in rows}
team_marks=','.join('?' for _ in team_ids)
team_alias_rows=[dict(r) for r in src.execute(f'SELECT * FROM team_aliases WHERE team_id IN ({team_marks})',team_ids)] if team_ids else []
for r in team_alias_rows:
 for col in ('valid_from_season_id','valid_to_season_id'):
  if r.get(col) is not None: season_ids.add(int(r[col]))
def rows_for(table, where, values):
 return [dict(r) for r in src.execute(f'SELECT * FROM {table} WHERE {where}',values)]
ref={
 'leagues': rows_for('leagues','league_id=?',(1,)),
 'seasons': rows_for('seasons','season_id IN ('+','.join('?' for _ in season_ids)+')',tuple(sorted(season_ids))),
 'teams': rows_for('teams',f'team_id IN ({team_marks})',tuple(team_ids)),
 'players': rows_for('players',f'player_id IN ({','.join('?' for _ in player_ids)})',tuple(player_ids)),
 'team_aliases': team_alias_rows,
 'player_aliases': rows_for('player_aliases',f'player_id IN ({','.join('?' for _ in player_ids)})',tuple(player_ids)),
 'player_external_ids': rows_for('player_external_ids',f'player_id IN ({','.join('?' for _ in player_ids)})',tuple(player_ids)),
 'team_external_ids': rows_for('team_external_ids',f'team_id IN ({team_marks})',tuple(team_ids)),
 'league_external_ids': rows_for('league_external_ids','league_id=?',(1,)),
 'season_external_ids': rows_for('season_external_ids',f'season_id IN ({','.join('?' for _ in season_ids)})',tuple(sorted(season_ids))),
}
dst=sqlite3.connect(TARGET)
dst.execute('PRAGMA foreign_keys=ON')
dst.executescript((ROOT/'database'/'schema.sql').read_text(encoding='utf-8'))
def insert_rows(table, data):
 cols=[r[1] for r in dst.execute(f'PRAGMA table_info({table})')]
 for row in data:
  use=[k for k in row if k in cols]
  dst.execute(f'INSERT INTO {table} ({",".join(chr(34)+k+chr(34) for k in use)}) VALUES ({",".join("?" for _ in use)})',tuple(row[k] for k in use))
for table in ('leagues','seasons','teams','players','player_aliases','player_external_ids','team_aliases','team_external_ids','league_external_ids','season_external_ids'):
 insert_rows(table,ref[table])
for r in rows:
 g={'game_id':r['game_id'],'league_id':r['league_id'],'season_id':r['season_id'],'game_date':r['game_date'],'home_team_id':r['home_team_id'],'away_team_id':r['away_team_id'],'venue':r['venue'],'attendance':r['attendance'],'home_score':None,'away_score':None,'status':'pending','competition_type':r['competition_type']}
 insert_rows('games',[g])
 insert_rows('source_livestats_games',[{k:r[k] for k in ('game_id','league_id','season_id','provider_game_id','match_id','json_url')}])
dst.commit()
violations=dst.execute('PRAGMA foreign_key_check').fetchall()
if violations: raise RuntimeError(f'Test seed FK violations: {len(violations)}')
print(f'DB={TARGET}')
print('selected='+','.join(MATCHES))
print('seed_counts='+str({k:len(v) for k,v in ref.items()}))
print('seeded_games='+str([{'game_id':r['game_id'],'match_id':r['match_id'],'season_id':r['season_id'],'home_team_id':r['home_team_id'],'away_team_id':r['away_team_id']} for r in rows]))
print('source_analytical_rows_copied=0')
dst.close();src.close()
