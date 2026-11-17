import {readFileSync,writeFileSync} from 'node:fs';
const p='scripts/leagues/estlatbl/mapping_recovery.mjs';
let s=readFileSync(p,'utf8');
const old='d.exec("PRAGMA foreign_keys=ON; BEGIN IMMEDIATE")';
const replacement="d.exec(\"PRAGMA foreign_keys=ON; BEGIN IMMEDIATE; INSERT OR IGNORE INTO seasons(season_id,label,start_year,end_year) VALUES(20212022,'2021-22',2021,2022)\")";
if(!s.includes(old))throw Error('target SQL not found');
writeFileSync(p,s.replace(old,replacement));
