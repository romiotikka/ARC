import assert from "node:assert/strict";
import { test } from "node:test";
import { validateMapping } from "../scripts/leagues/estlatbl/mapping_recovery.mjs";

const row={provider_game_id:"2022212804",season_id:"20212022",game_date:"2022-04-10",home_team:"VEF Rīga",away_team:"KK Viimsi/Sportland",home_score:"95",away_score:"64",match_id:"test-id",source_url:"manual source",notes:"keep this note",verification_status:"UNVERIFIED"};
function replies({teams=[{name:"VEF RIGA",score:95},{name:"KK Viimsi/Sportland",score:64}],date="10/4/22",competition="Paf Estonian-Latvian Basketball League"}={}) {
  return async url=>url.includes("data.json")?new Response(JSON.stringify({tm:{1:teams[0],2:teams[1]}}),{status:200}):new Response(`<h6>Competition</h6><p>${competition}</p><h6>Game Details</h6><p>Tip off: 5:00 pm GMT ${date}</p><p>${teams[0].name} vs ${teams[1].name}</p>`,{status:200});
}

test("verifies matching FIBA teams, date, score and competition without losing manual notes",async()=>{
  const out=await validateMapping(row,replies());assert.equal(out.verification_status,"VERIFIED");assert.equal(out.notes,row.notes);assert.equal(out.source_url,row.source_url);
});
test("accepts reversed team order only when score pair is reversed too",async()=>{
  const out=await validateMapping(row,replies({teams:[{name:"KK Viimsi/Sportland",score:64},{name:"VEF RIGA",score:95}]}));assert.equal(out.verification_status,"VERIFIED");
});
test("rejects wrong teams, date, score or competition",async()=>{
  for(const options of [
    {teams:[{name:"Wrong Club",score:95},{name:"KK Viimsi/Sportland",score:64}]},
    {date:"11/4/22"},{teams:[{name:"VEF RIGA",score:94},{name:"KK Viimsi/Sportland",score:64}]},
    {competition:"Other Competition"},
  ]) assert.equal((await validateMapping(row,replies(options))).verification_status,"CONFLICT");
});
test("keeps incomplete metadata UNVERIFIED and unavailable endpoints UNAVAILABLE",async()=>{
  assert.equal((await validateMapping(row,async()=>new Response(JSON.stringify({tm:{1:{name:"VEF",score:95}}}),{status:200}))).verification_status,"UNVERIFIED");
  assert.equal((await validateMapping(row,async()=>{throw Error("network down")})).verification_status,"UNAVAILABLE");
});
test("does not validate blank IDs and rejects non-2021–22 rows",async()=>{
  assert.equal((await validateMapping({...row,match_id:""})).verification_status,"UNVERIFIED");
  assert.equal((await validateMapping({...row,season_id:"20222023"},replies())).verification_status,"CONFLICT");
});
