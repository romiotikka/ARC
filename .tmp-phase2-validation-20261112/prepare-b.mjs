import { DatabaseSync } from 'node:sqlite';
const db = new DatabaseSync(process.env.ARC_DATABASE_PATH);
if (db.prepare('SELECT 1 FROM games WHERE game_id=?').get('validation-2155684')) throw new Error('B game already exists');
if (db.prepare('SELECT 1 FROM source_livestats_games WHERE match_id=?').get('2155684')) throw new Error('B mapping already exists');
db.prepare('INSERT INTO seasons(season_id,label,start_year,end_year) VALUES(20232024,?,?,?)').run('2023-24',2023,2024);
db.prepare('INSERT INTO games(game_id,league_id,season_id,status) VALUES(?,1,20232024,?)').run('validation-2155684','pending');
db.prepare('INSERT INTO source_livestats_games(game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES(?,1,20232024,?,?,?)').run('validation-2155684','2155684','2155684','https://fibalivestats.dcd.shared.geniussports.com/data/2155684/data.json');
db.close();
console.log('Seeded only disposable Test B mapping: validation-2155684 -> LiveStats 2155684');
