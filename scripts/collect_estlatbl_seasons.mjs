import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = join(__dirname, "..");
const databasePath = join(rootDir, "data", "arc.db");

const LEAGUE_ID = "estlatbl";

const DEFAULT_SEASONS = [
  { setSid: 2026, seasonId: 20252026, label: "2025-26", startYear: 2025, endYear: 2026 },
  { setSid: 2025, seasonId: 20242025, label: "2024-25", startYear: 2024, endYear: 2025 },
  { setSid: 2024, seasonId: 20232024, label: "2023-24", startYear: 2023, endYear: 2024 },
  { setSid: 2023, seasonId: 20222023, label: "2022-23", startYear: 2022, endYear: 2023 },
];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchText(url) {
  const response = await fetch(url);

  if (!response.ok) {
    throw new Error(`Request failed ${response.status} ${response.statusText}: ${url}`);
  }

  return response.text();
}

function parseGameIds(html) {
  return [...html.matchAll(/\/et\/tulemused\/(\d+)\/#c/g)]
    .map((match) => match[1])
    .filter((value, index, values) => values.indexOf(value) === index)
    .sort();
}

function parseMatchId(html) {
  return html.match(/matchId=(\d+)/)?.[1] ?? null;
}

function upsertLeague(database) {
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
      LEAGUE_ID,
      "Estonian-Latvian Basketball League",
      "Estonia/Latvia",
      "Northern Europe",
    );
}

function upsertSeason(database, season) {
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
    .run(season.seasonId, season.label, season.startYear, season.endYear);
}

function upsertGameMapping(database, season, providerGameId, matchId) {
  const gameId = `${LEAGUE_ID}_${providerGameId}`;
  const jsonUrl = `https://fibalivestats.dcd.shared.geniussports.com/data/${matchId}/data.json`;

  database
    .prepare(
      `
      INSERT INTO games (game_id, league_id, season_id, status)
      VALUES (?, ?, ?, ?)
      ON CONFLICT (game_id) DO UPDATE SET
        league_id = excluded.league_id,
        season_id = excluded.season_id
      `,
    )
    .run(gameId, LEAGUE_ID, season.seasonId, "scheduled_or_import_pending");

  database
    .prepare(
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
    )
    .run(gameId, LEAGUE_ID, season.seasonId, String(providerGameId), String(matchId), jsonUrl);
}

async function collectSeason(database, season) {
  const seasonUrl = `https://www.estlatbl.com/et/tulemused?setSid=${season.setSid}`;
  const html = await fetchText(seasonUrl);
  const gameIds = parseGameIds(html);

  let matched = 0;
  let missing = 0;

  upsertSeason(database, season);

  for (const gameId of gameIds) {
    const liveStatsUrl = `https://www.estlatbl.com/et/tulemused/${gameId}/live-stats`;
    const liveStatsHtml = await fetchText(liveStatsUrl);
    const matchId = parseMatchId(liveStatsHtml);

    if (!matchId) {
      missing += 1;
      continue;
    }

    upsertGameMapping(database, season, gameId, matchId);
    matched += 1;

    await sleep(100);
  }

  return {
    season: season.label,
    gameIds: gameIds.length,
    matched,
    missing,
  };
}

const database = new DatabaseSync(databasePath);
database.exec("PRAGMA foreign_keys = ON;");

try {
  database.exec("BEGIN;");
  upsertLeague(database);
  database.exec("COMMIT;");

  for (const season of DEFAULT_SEASONS) {
    database.exec("BEGIN;");

    try {
      const result = await collectSeason(database, season);
      database.exec("COMMIT;");
      console.log(
        `${result.season}: game_ids=${result.gameIds}, mappings=${result.matched}, missing_match_id=${result.missing}`,
      );
    } catch (error) {
      database.exec("ROLLBACK;");
      throw error;
    }
  }
} finally {
  database.close();
}

