import {readFileSync,writeFileSync} from 'node:fs';
const p='data/estlatbl_2021_22_mapping_overrides.csv';let s=readFileSync(p,'utf8');
const before='"2022212804","20212022","2022-04-10","VEF Rīga","KK Viimsi/Sportland","95","64","","","","UNVERIFIED"';
const after='"2022212804","20212022","2022-04-10","VEF Rīga","KK Viimsi/Sportland","95","64","2082683","https://www.estlatbl.com/et/tulemused/2022212804/live-stats","Match ID observed in user-verified Chrome session.","UNVERIFIED"';
if(!s.includes(before))throw Error('Expected manifest row not found');writeFileSync(p,s.replace(before,after));
