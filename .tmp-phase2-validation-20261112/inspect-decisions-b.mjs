import { DatabaseSync } from 'node:sqlite';
const db=new DatabaseSync(process.env.ARC_DATABASE_PATH,{readOnly:true});
const n=2;
const sets=db.prepare('SELECT decision_set_id,decision_set_version,resolver_version,decision_set_sha256,status FROM identity_decision_sets WHERE normalization_id=? ORDER BY decision_set_version').all(n);
const candidates=db.prepare('SELECT candidate_id,decision_set_id,candidate_json FROM source_import_analytical_candidates WHERE normalization_id=? ORDER BY candidate_id').all(n);
const summarize=c=>({candidate_id:c.candidate_id,decision_set_id:c.decision_set_id,hash:JSON.parse(c.candidate_json).decision_set_sha256,decisions:JSON.parse(c.candidate_json).identity_decisions.map(d=>({occurrence_id:d.player_occurrence_id,player_id:d.arc_player_id,status:d.status,source:d.decision_source,evidence:d.evidence_json})).slice(0,2)});
console.log(JSON.stringify({sets,candidates:candidates.map(summarize)},null,2));db.close();
