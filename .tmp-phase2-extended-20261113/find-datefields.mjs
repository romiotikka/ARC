import { DatabaseSync } from 'node:sqlite';import {gunzipSync} from 'node:zlib';
const d=new DatabaseSync(process.env.ARC_EXTENDED_DB,{readOnly:true});
function find(v,path='',out=[]){if(Array.isArray(v)){v.forEach((x,i)=>find(x,`${path}[${i}]`,out));}else if(v&&typeof v==='object'){for(const[k,x]of Object.entries(v)){if(/date|datetime|starttime|gametime/i.test(k))out.push({path:path?`${path}.${k}`:k,value:x});find(x,path?`${path}.${k}`:k,out);}}return out;}
const rows=d.prepare('SELECT s.provider_game_id,n.normalized_json_gzip FROM source_game_snapshots s JOIN source_snapshot_normalizations n USING(snapshot_id)').all();for(const r of rows){const x=JSON.parse(gunzipSync(r.normalized_json_gzip));console.log(JSON.stringify({match:r.provider_game_id,fields:find(x)}));}d.close();
