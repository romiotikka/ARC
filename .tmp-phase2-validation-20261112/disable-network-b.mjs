import { DatabaseSync } from 'node:sqlite';
const db=new DatabaseSync(process.env.ARC_DATABASE_PATH);
db.prepare("UPDATE source_livestats_games SET json_url='mock://provider-unavailable' WHERE game_id=?").run('validation-2155684');
db.close();
