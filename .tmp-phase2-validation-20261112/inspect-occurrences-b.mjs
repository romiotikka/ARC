import { DatabaseSync } from 'node:sqlite';
const d=new DatabaseSync(process.env.ARC_DATABASE_PATH,{readOnly:true});
console.log(JSON.stringify(d.prepare("SELECT original_name,provider_player_id,pno,jersey_number,roster_entry_key,team_occurrence_id FROM source_player_occurrences WHERE normalization_id=2 ORDER BY source_ordinal LIMIT 6").all(),null,2));d.close();

