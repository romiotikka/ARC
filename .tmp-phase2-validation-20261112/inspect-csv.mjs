import { readFileSync } from 'node:fs';
const rows=readFileSync('data/estlatbl/estlatbl_2026_games.csv','utf8').trim().split(/\r?\n/).slice(1).map(line=>{const [game_id,match_id,json_url]=line.split(',');return {game_id,match_id,json_url}});
console.log(JSON.stringify({count:rows.length,sample:[...rows.slice(0,12),...rows.slice(-8)]},null,2));
