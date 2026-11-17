import { DatabaseSync } from 'node:sqlite';
const d=new DatabaseSync(process.env.ARC_EXTENDED_DB,{readOnly:true});
const cs=d.prepare('SELECT candidate_id,candidate_json FROM source_import_analytical_candidates WHERE normalization_id=? ORDER BY candidate_id').all(2).map(r=>({id:r.candidate_id,b:JSON.parse(r.candidate_json)}));
const a=cs.find(x=>x.id===2).b,b=cs.find(x=>x.id===7).b;
function compare(x,y,path='',out=[]){if(JSON.stringify(x)===JSON.stringify(y))return out;if(Array.isArray(x)&&Array.isArray(y)){if(x.length!==y.length){out.push({path,len1:x.length,len2:y.length});return out;}for(let i=0;i<x.length;i++)compare(x[i],y[i],`${path}[${i}]`,out);return out;}if(x&&y&&typeof x==='object'&&typeof y==='object'){for(const k of new Set([...Object.keys(x),...Object.keys(y)]))compare(x[k],y[k],path?`${path}.${k}`:k,out);return out;}out.push({path,a:x,b:y});return out;}
const dff=compare({...a,projection:undefined},{...b,projection:undefined});
console.log(JSON.stringify({candidates:cs.map(x=>({id:x.id,status:x.b.decision_set_status,hash:x.b.decision_set_sha256,version:x.b.resolver_version,base:x.b.identity_base_sha256})),bundle_diffs:dff.slice(0,30),diff_count:dff.length},null,2));d.close();
