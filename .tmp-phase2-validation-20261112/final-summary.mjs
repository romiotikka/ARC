import { DatabaseSync } from 'node:sqlite';
const db=new DatabaseSync(process.env.ARC_DATABASE_PATH,{readOnly:true});
const summary={database:process.env.ARC_DATABASE_PATH,games:{}};
for(const game of ['2026212024','validation-2155684']){
 const active=db.prepare('SELECT snapshot_id,normalization_id FROM active_game_source_snapshots WHERE game_id=?').get(game);
 const identity=db.prepare('SELECT decision_set_id FROM active_identity_decision_sets WHERE game_id=?').get(game);
 const c=db.prepare("SELECT candidate_id,import_run_id,status,snapshot_id,normalization_id FROM source_import_analytical_candidates WHERE game_id=? ORDER BY candidate_id DESC LIMIT 1").get(game);
 const run=db.prepare('SELECT rr.reconstruction_run_id,rr.status,rr.input_sha256,rr.output_sha256 FROM active_reconstructions a JOIN reconstruction_runs rr USING(reconstruction_run_id) WHERE a.game_id=?').get(game);
 summary.games[game]={active,candidate:c,identity,reconstruction:run,counts:Object.fromEntries(['team_games','player_games','events','lineup_segments'].map(t=>[t,db.prepare(`SELECT COUNT(*) n FROM ${t} WHERE game_id=?`).get(game).n])),captured_hash:db.prepare('SELECT captured_body_sha256 FROM source_game_snapshots WHERE snapshot_id=?').get(active.snapshot_id).captured_body_sha256};
}
summary.failed_test_candidate=db.prepare("SELECT status,error_message FROM source_import_analytical_candidates WHERE game_id='validation-2155684' ORDER BY candidate_id DESC LIMIT 2").all();
summary.fk_violations=db.prepare('PRAGMA foreign_key_check').all().length;
summary.production_path_opened=false;
console.log(JSON.stringify(summary,null,2));db.close();
