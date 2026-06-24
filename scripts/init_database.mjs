import { readFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = join(__dirname, "..");
const databaseDir = join(rootDir, "data");
const databasePath = join(databaseDir, "arc.db");
const schemaPath = join(rootDir, "database", "schema.sql");
const estlatblGamesCsv = join(rootDir, "estlatbl_2026_games.csv");

const ESTLATBL_LEAGUE_ID = "estlatbl";
const ESTLATBL_SEASON_ID = 20252026;

function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/);
  const headers = lines[0].split(",");

  return lines.slice(1).map((line) => {
    const values = line.split(",");
    return Object.fromEntries(headers.map((header, index) => [header, values[index]]));
  });
}

function tableCount(database, tableName) {
  return database.prepare(`SELECT COUNT(*) AS count FROM ${tableName}`).get().count;
}

mkdirSync(databaseDir, { recursive: true });

const database = new DatabaseSync(databasePath);
database.exec("PRAGMA foreign_keys = ON;");
database.exec(readFileSync(schemaPath, "utf8"));

database.exec("BEGIN;");

try {
  database
    .prepare(
      `
      INSERT INTO leagues (league_id, name, country, region)
      VALUES (?, ?, ?, ?)
      ON CONFLICT (league_id) DO UPDATE SET
        name = excluded.name,
        country = excluded.country,
        region = excluded.region
      `,
    )
    .run(
      ESTLATBL_LEAGUE_ID,
      "Estonian-Latvian Basketball League",
      "Estonia/Latvia",
      "Northern Europe",
    );

  database
    .prepare(
      `
      INSERT INTO seasons (season_id, label, start_year, end_year)
      VALUES (?, ?, ?, ?)
      ON CONFLICT (season_id) DO UPDATE SET
        label = excluded.label,
        start_year = excluded.start_year,
        end_year = excluded.end_year
      `,
    )
    .run(ESTLATBL_SEASON_ID, "2025-26", 2025, 2026);

  const insertGame = database.prepare(
    `
    INSERT INTO games (game_id, league_id, season_id, status)
    VALUES (?, ?, ?, ?)
    ON CONFLICT (game_id) DO UPDATE SET
      league_id = excluded.league_id,
      season_id = excluded.season_id
    `,
  );

  const insertLivestatsMapping = database.prepare(
    `
    INSERT INTO source_livestats_games (
      game_id,
      league_id,
      season_id,
      provider_game_id,
      match_id,
      json_url
    )
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (game_id) DO UPDATE SET
      league_id = excluded.league_id,
      season_id = excluded.season_id,
      provider_game_id = excluded.provider_game_id,
      match_id = excluded.match_id,
      json_url = excluded.json_url
    `,
  );

  const rows = parseCsv(readFileSync(estlatblGamesCsv, "utf8"));

  for (const row of rows) {
    const providerGameId = row.game_id;
    const gameId = `${ESTLATBL_LEAGUE_ID}_${providerGameId}`;

    insertGame.run(
      gameId,
      ESTLATBL_LEAGUE_ID,
      ESTLATBL_SEASON_ID,
      "scheduled_or_import_pending",
    );

    insertLivestatsMapping.run(
      gameId,
      ESTLATBL_LEAGUE_ID,
      ESTLATBL_SEASON_ID,
      providerGameId,
      row.match_id,
      row.json_url,
    );
  }

  database.exec("COMMIT;");

  const tables = [
    "leagues",
    "seasons",
    "games",
    "source_livestats_games",
    "teams",
    "players",
    "team_games",
    "player_games",
    "events",
  ];

  console.log(`Database: ${databasePath}`);
  console.log(`Imported EstLatBL LiveStats mappings: ${rows.length}`);
  console.log("");
  console.log("Table counts:");
  for (const table of tables) {
    console.log(`- ${table}: ${tableCount(database, table)}`);
  }
} catch (error) {
  database.exec("ROLLBACK;");
  throw error;
} finally {
  database.close();
}

