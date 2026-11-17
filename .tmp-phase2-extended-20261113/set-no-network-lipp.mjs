import { DatabaseSync } from 'node:sqlite';
const d=new DatabaseSync(process.env.ARC_DATABASE_PATH);d.prepare("UPDATE source_livestats_games SET json_url='mock://provider-unavailable' WHERE match_id='2155618'").run();d.close();
