import { DatabaseSync } from 'node:sqlite';
import { readFileSync, writeFileSync } from 'node:fs';
import { parseEstlatblResultsPage } from '../scripts/leagues/estlatbl/results.mjs';
const dir='.tmp-2021-22-mapping-audit-20261115';
const db=new DatabaseSync('data/arc2.db',{readOnly:true});
let image; try {image=db.serialize()} finally {db.close()}
writeFileSync(`${dir}/arc2-disposable.sqlite`,image,{flag:'wx'});
const captured='C:/Users/Kasutaja/Downloads/Tulemused_21_22.html';
const parsed=parseEstlatblResultsPage(readFileSync(captured,'utf8'));
const refResponse=await fetch('https://fibalivestats.dcd.shared.geniussports.com/data/1999499/data.json',{signal:AbortSignal.timeout(15000)});
let ref=null;
if(refResponse.ok){const j=await refResponse.json();ref={status:refResponse.status,validJson:true,teams:Object.fromEntries(Object.entries(j.tm??{}).map(([k,v])=>[k,{name:v.name,score:v.score}]))}}
const games=parsed.records.map(r=>({providerGameId:r.providerGameId,date:r.gameDate,homeTeam:r.homeTeam,awayTeam:r.awayTeam,officialScore:[r.homeScore,r.awayScore],completed:r.completed,lookupStatus:r.providerGameId==='2022212804'?'blocked_security_checkpoint':'not_attempted_after_checkpoint',matchId:null,jsonStatus:'not_checked',reason:r.providerGameId==='2022212804'?'Official game and /live-stats requests returned Turvakontroll Turnstile checkpoint.':'Not requested: same official host blocked the reference per-game request; no challenge bypass attempted.'}));
const refGame=games.find(g=>g.providerGameId==='2022212804');
if(refGame){refGame.claimedMatchId=1999499;refGame.candidateMappingVerification=ref?{status:'conflict',httpStatus:ref.status,validJson:ref.validJson,fibaTeams:ref.teams,reason:'FIBA JSON is TalTech/OPTIBET 94–62 Latvijas Universitate; does not match VEF Rīga 95–64 KK Viimsi/Sportland.'}:{status:'unverified',reason:'FIBA JSON request did not return a valid record.'};}
const report={createdAt:new Date().toISOString(),sourceHtml:captured,disposableDatabase:`${dir}/arc2-disposable.sqlite`,scope:'2021-22 EstLatBL completed games',summary:{discovered:parsed.discovered,completed:parsed.records.length,parseIssues:parsed.issues.length,parsedDuplicateGameIds:parsed.issues.filter(x=>x.reasons.includes('duplicate_provider_game_id_in_results_page')).length,confirmedMappings:0,confirmedMissingMatchId:0,confirmedInaccessiblePages:1,remainingPerGameLookupsNotAttempted:Math.max(0,parsed.records.length-1),verifiedJsonForCorrectGame:0,knownClaimedMappingConflicts:refGame?.candidateMappingVerification?.status==='conflict'?1:0},access:{seasonResultsPage:{httpStatus:200,bodyTitle:'Turvakontroll',turnstile:true},referenceGamePage:{httpStatus:200,bodyTitle:'Turvakontroll',turnstile:true},referenceLiveStatsPage:{httpStatus:200,bodyTitle:'Turvakontroll',turnstile:true},fibaJson1999499:ref},games};
writeFileSync(`${dir}/mapping-audit.json`,JSON.stringify(report,null,2));
console.log(JSON.stringify({report:`${dir}/mapping-audit.json`,disposableDatabase:`${dir}/arc2-disposable.sqlite`,summary:report.summary,reference:refGame?.candidateMappingVerification}));

