import { DatabaseSync } from 'node:sqlite';
import { gunzipSync } from 'node:zlib';
const db=new DatabaseSync(process.env.ARC_DATABASE_PATH,{readOnly:true});
const row=db.prepare("SELECT payload_gzip FROM source_game_snapshots WHERE game_id='validation-2155684' ORDER BY snapshot_id DESC LIMIT 1").get();
const data=JSON.parse(gunzipSync(row.payload_gzip).toString('utf8'));
console.log(JSON.stringify(Object.entries(data.tm?.['1']?.pl??{}).filter(([,p])=>Number(p.starter)===1).map(([key,p])=>({key,name:p.name,shirtNumber:p.shirtNumber,playerId:p.playerId})),null,2));
db.close();
