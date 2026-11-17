import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
const db=new DatabaseSync(process.env.ARC_DATABASE_PATH,{readOnly:true});
const game=process.argv[2];
const c=db.prepare('SELECT * FROM source_import_analytical_candidates WHERE game_id=? ORDER BY candidate_id DESC LIMIT 1').get(game);
if(!c){console.log(JSON.stringify({game,candidate:null}));process.exit(2)}
const s=db.prepare('SELECT * FROM source_game_snapshots WHERE snapshot_id=?').get(c.snapshot_id);
const n=db.prepare('SELECT * FROM source_snapshot_normalizations WHERE normalization_id=?').get(c.normalization_id);
const raw=gunzipSync(s.payload_gzip), normalized=gunzipSync(n.normalized_json_gzip);
const counts=Object.fromEntries(['team_games','player_games','events'].map(t=>[t,db.prepare(`SELECT COUNT(*) n FROM ${t} WHERE game_id=?`).get(game).n]));
const projection=JSON.parse(c.candidate_json).projection;
const projected={team_games:projection.team_games.length,player_games:projection.player_games.length,events:projection.events.length};
const decisions=db.prepare('SELECT status,COUNT(*) n FROM identity_decisions WHERE decision_set_id=? GROUP BY status').all(c.decision_set_id);
const run=db.prepare('SELECT rr.* FROM active_reconstructions a JOIN reconstruction_runs rr USING(reconstruction_run_id) WHERE a.game_id=?').get(game);
const segs=db.prepare('SELECT ls.*,l.player_1,l.player_2,l.player_3,l.player_4,l.player_5 FROM lineup_segments ls JOIN lineups l USING(lineup_id) WHERE ls.game_id=?').all(game);
const invalid=segs.filter(x=>new Set([x.player_1,x.player_2,x.player_3,x.player_4,x.player_5]).size!==5).length;
const out={game_id:game,candidate:{candidate_id:c.candidate_id,status:c.status,snapshot_id:c.snapshot_id,normalization_id:c.normalization_id,decision_set_id:c.decision_set_id},source:{match_id:s.provider_game_id,url:s.source_url,hash_ok:createHash('sha256').update(raw).digest('hex')===s.captured_body_sha256,normalized_hash_ok:createHash('sha256').update(normalized).digest('hex')===n.normalized_sha256,normalized_status:n.status,occurrences:{teams:db.prepare('SELECT COUNT(*) n FROM source_team_occurrences WHERE normalization_id=?').get(c.normalization_id).n,players:db.prepare('SELECT COUNT(*) n FROM source_player_occurrences WHERE normalization_id=?').get(c.normalization_id).n,events:db.prepare('SELECT COUNT(*) n FROM source_event_occurrences WHERE normalization_id=?').get(c.normalization_id).n}},decisions,counts,projected,activeSource:db.prepare('SELECT snapshot_id,normalization_id FROM active_game_source_snapshots WHERE game_id=?').get(game),activeIdentity:db.prepare('SELECT normalization_id,decision_set_id FROM active_identity_decision_sets WHERE game_id=?').get(game),reconstruction:run&&{id:run.reconstruction_run_id,status:run.status,input_sha256:run.input_sha256,output_sha256:run.output_sha256},lineup_segments:segs.length,invalid_five:invalid,fk_violations:db.prepare('PRAGMA foreign_key_check').all().length,duplicate_player_game_keys:db.prepare('SELECT COUNT(*) n FROM (SELECT 1 FROM player_games WHERE game_id=? GROUP BY player_id,team_id,shirt_number HAVING COUNT(*)>1)').get(game).n};
console.log(JSON.stringify(out,null,2));db.close();
