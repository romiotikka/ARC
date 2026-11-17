import { DatabaseSync } from 'node:sqlite';
const d=new DatabaseSync(process.env.ARC_EXTENDED_DB,{readOnly:true});
const n=2;const sets=d.prepare('SELECT decision_set_id,decision_set_version,decision_set_sha256 FROM identity_decision_sets WHERE normalization_id=? ORDER BY decision_set_version').all(n);
let diff=[];
if(sets.length>1){diff=d.prepare(`SELECT a.player_occurrence_id,a.arc_player_id old_id,b.arc_player_id new_id,a.status old_status,b.status new_status,a.decision_source old_source,b.decision_source new_source,a.evidence_json old_evidence,b.evidence_json new_evidence FROM identity_decisions a JOIN identity_decisions b USING(normalization_id,player_occurrence_id) WHERE a.decision_set_id=? AND b.decision_set_id=? AND (a.arc_player_id IS NOT b.arc_player_id OR a.status IS NOT b.status OR a.decision_source IS NOT b.decision_source OR a.evidence_json IS NOT b.evidence_json)`,[sets[0].decision_set_id,sets.at(-1).decision_set_id]).all();}
console.log(JSON.stringify({sets,differing_decision_rows:diff.length,sample:diff.slice(0,4)},null,2));d.close();
