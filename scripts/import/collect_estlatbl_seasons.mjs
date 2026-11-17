import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { persistValidatedGameDate } from "../core/game_dates.mjs";
import { classifyEstlatblResultMetadataIssues, findExistingEstlatblMapping, validateEstlatblMapping, validateEstlatblResultMetadata } from "../leagues/estlatbl/matching.mjs";
import { parseEstlatblResultsPage } from "../leagues/estlatbl/results.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = join(__dirname, "..", "..");
const databasePath = process.env.ARC_DATABASE_PATH ?? join(rootDir, "data", "arc2.db");

const LEAGUE_KEY = "estlatbl";
const LEAGUE_ID = 1;

const DEFAULT_SEASONS = [
  { setSid: 2026, seasonId: 20252026, label: "2025-26", startYear: 2025, endYear: 2026 },
  { setSid: 2025, seasonId: 20242025, label: "2024-25", startYear: 2024, endYear: 2025 },
  { setSid: 2024, seasonId: 20232024, label: "2023-24", startYear: 2023, endYear: 2024 },
  { setSid: 2023, seasonId: 20222023, label: "2022-23", startYear: 2022, endYear: 2023 },
  { setSid: 2022, seasonId: 20212022, label: "2021-22", startYear: 2021, endYear: 2022 },
];

// Verified official EstLatBL result 85–102 differs from FIBA-derived ARC
// 80–102 for this exact mapped game. Preserve both sources and permit this
// documented discrepancy only; all other score mismatches remain blockers.
const KNOWN_SCORE_DISCREPANCIES = new Map([
  ["estlatbl_2023212099", { matchId: "2155636", official: [85, 102], arc: [80, 102] }],
]);

function parseArgs(argv) {
  const args = {
    recent: false,
    recentCount: 2,
  };

  for (let index = 2; index < argv.length; index += 1) {
    const arg = argv[index];

    if (arg === "--recent") {
      args.recent = true;
    } else if (arg === "--recent-count") {
      args.recentCount = Number(argv[index + 1]);
      index += 1;
    }
  }

  return args;
}

function selectSeasons(seasons, args) {
  if (!args.recent) {
    return seasons;
  }

  return seasons.slice(0, args.recentCount);
}

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

function upsertGameMapping(database, season, providerGameId, matchId, status = "scheduled_or_import_pending") {
  const gameId = `${LEAGUE_KEY}_${providerGameId}`;
  const jsonUrl = `https://fibalivestats.dcd.shared.geniussports.com/data/${matchId}/data.json`;

  database
    .prepare(
      `
      INSERT INTO games (game_id, league_id, season_id, status)
      VALUES (?, ?, ?, ?)
      ON CONFLICT (game_id) DO UPDATE SET
        league_id = excluded.league_id,
        season_id = excluded.season_id,
        status = CASE
          WHEN excluded.status = 'scheduled_or_import_pending'
            AND games.status IN ('boxscore_imported', 'boxscore_imported_events_only')
          THEN games.status
          ELSE excluded.status
        END
      `,
    )
    .run(gameId, LEAGUE_ID, season.seasonId, status);

  database
    .prepare(
      `
      UPDATE source_livestats_games
      SET
        league_id = ?,
        season_id = ?,
        provider_game_id = ?,
        match_id = ?,
        json_url = ?
      WHERE game_id = ?
      `,
    )
    .run(LEAGUE_ID, season.seasonId, String(providerGameId), String(matchId), jsonUrl, gameId);

  database
    .prepare(
      `
      INSERT OR IGNORE INTO source_livestats_games (
        game_id,
        league_id,
        season_id,
        provider_game_id,
        match_id,
        json_url
      )
      VALUES (?, ?, ?, ?, ?, ?)
      `,
    )
    .run(gameId, LEAGUE_ID, season.seasonId, String(providerGameId), String(matchId), jsonUrl);

  // Ensure existing rows keyed by match_id are also refreshed without crashing on uniqueness conflicts.
  database
    .prepare(
      `
      UPDATE source_livestats_games
      SET
        league_id = ?,
        season_id = ?,
        provider_game_id = ?,
        json_url = ?
      WHERE match_id = ?
      `,
    )
    .run(LEAGUE_ID, season.seasonId, String(providerGameId), jsonUrl, String(matchId));
}

async function collectSeason(database, season) {
  const seasonUrl = `https://www.estlatbl.com/et/tulemused?setSid=${season.setSid}`;
  const html = await fetchText(seasonUrl);
  const { records, issues, discovered, flaggedRows, notPlayedRecords = [] } = parseEstlatblResultsPage(html);

  let matched = 0;
  let missing = 0;
  let datesPopulated = 0;
  let datesUnchanged = 0;
  let skipped = flaggedRows;
  let review = flaggedRows;
  let conflicts = 0;
  let scoreDiscrepancies = 0;
  let errors = flaggedRows;
  let notPlayed = notPlayedRecords.length;
  let scheduled = 0;

  for (const issue of issues) {
    console.warn(`review season=${season.label} provider_game=${issue.providerGameId ?? "unknown"}: ${issue.reasons.join(",")}`);
  }

  upsertSeason(database, season);

  for (const result of notPlayedRecords) {
    const gameId = result.providerGameId;
    const arcGameId = `${LEAGUE_KEY}_${gameId}`;
    const existingMapping = findExistingEstlatblMapping(database, arcGameId, gameId, season);
    if (existingMapping.status === "conflict") {
      review += 1;
      skipped += 1;
      conflicts += 1;
      console.warn(`review ${arcGameId}: ${existingMapping.issues.join(",")}`);
      continue;
    }
    if (existingMapping.status === "matched") {
      upsertGameMapping(database, season, gameId, existingMapping.matchId, "not_played");
    }
    skipped += 1;
    console.log(`not played ${arcGameId}: official results page marks “mängu ei toimu”`);
  }

  for (const result of records) {
    const gameId = result.providerGameId;
    const arcGameId = `${LEAGUE_KEY}_${gameId}`;
    const existingMapping = findExistingEstlatblMapping(database, arcGameId, gameId, season);
    if (existingMapping.status === "conflict") {
      review += 1;
      skipped += 1;
      conflicts += 1;
      console.warn(`review ${arcGameId}: ${existingMapping.issues.join(",")}`);
      continue;
    }

    let matchId = existingMapping.status === "matched" ? existingMapping.matchId : null;
    if (!matchId) {
      const liveStatsUrl = `https://www.estlatbl.com/et/tulemused/${gameId}/live-stats`;
      try {
        const liveStatsHtml = await fetchText(liveStatsUrl);
        matchId = parseMatchId(liveStatsHtml);
      } catch (error) {
        missing += 1;
        errors += 1;
        skipped += 1;
        console.error(`error ${arcGameId}: live_stats_lookup_failed: ${error.message}`);
        continue;
      }
    }

    if (!result.completed) {
      const mappingIssues = validateEstlatblMapping(database, season, arcGameId, gameId, matchId);
      if (mappingIssues.length) {
        review += 1;
        skipped += 1;
        conflicts += 1;
        console.warn(`review ${arcGameId}: ${mappingIssues.join(",")}`);
        continue;
      }
      upsertGameMapping(database, season, gameId, matchId, "scheduled");
      matched += 1;
      scheduled += 1;
      skipped += 1;
      continue;
    }

    if (!matchId) {
      missing += 1;
      skipped += 1;
      errors += 1;
      console.error(`error ${arcGameId}: missing_fiba_match_id_from_official_live_stats_page`);
      continue;
    }

    const mappingIssues = validateEstlatblMapping(database, season, arcGameId, gameId, matchId);
    if (mappingIssues.length) {
      review += 1;
      skipped += 1;
      conflicts += 1;
      console.warn(`review ${arcGameId}: ${mappingIssues.join(",")}`);
      continue;
    }

    const metadataIssues = validateEstlatblResultMetadata(database, arcGameId, season, result);
    const existingScores = database.prepare("SELECT home_score, away_score FROM games WHERE game_id = ?").get(arcGameId);
    const knownScore = KNOWN_SCORE_DISCREPANCIES.get(arcGameId);
    const permitsKnownScoreDiscrepancy = Boolean(
      knownScore && matchId === knownScore.matchId &&
      result.homeScore === knownScore.official[0] && result.awayScore === knownScore.official[1] &&
      existingScores?.home_score === knownScore.arc[0] && existingScores?.away_score === knownScore.arc[1]
    );
    const classifiedMetadata = classifyEstlatblResultMetadataIssues(metadataIssues, {
      allowKnownScoreDiscrepancy: permitsKnownScoreDiscrepancy,
    });
    if (classifiedMetadata.conflicts.length) {
      review += 1;
      skipped += 1;
      conflicts += 1;
      console.warn(`review ${arcGameId}: ${classifiedMetadata.conflicts.join(",")}`);
      continue;
    }
    if (classifiedMetadata.scoreDiscrepancies.length) {
      scoreDiscrepancies += 1;
      console.warn(`known score discrepancy ${arcGameId}: official_estlatbl=${result.homeScore}-${result.awayScore}; existing_arc_fiba=${existingScores?.home_score ?? "null"}-${existingScores?.away_score ?? "null"}; fields=${classifiedMetadata.scoreDiscrepancies.join(",")}`);
    }

    upsertGameMapping(database, season, gameId, matchId, "scheduled_or_import_pending");
    matched += 1;

    if (!result.completed) {
      skipped += 1;
      continue;
    }

    const persisted = persistValidatedGameDate(database, arcGameId, result.gameDate);
    if (persisted.status === "updated") datesPopulated += 1;
    else if (persisted.status === "unchanged") datesUnchanged += 1;
    else {
      review += 1;
      skipped += 1;
      if (persisted.status === "conflict") conflicts += 1;
      else errors += 1;
      console.warn(`review ${arcGameId}: game_date_${persisted.status}`);
    }

    await sleep(100);
  }

  return {
    season: season.label,
    resultRecords: discovered,
    notPlayed,
    scheduled,
    matched,
    missing,
    datesPopulated,
    datesUnchanged,
    conflicts,
    scoreDiscrepancies,
    errors,
    skipped,
    review,
  };
}

const args = parseArgs(process.argv);
const seasons = selectSeasons(DEFAULT_SEASONS, args);

const database = new DatabaseSync(databasePath);
database.exec("PRAGMA foreign_keys = ON;");

try {
  database.exec("BEGIN;");
  upsertLeague(database);
  database.exec("COMMIT;");

  const totals = {
    resultRecords: 0,
    notPlayed: 0,
    scheduled: 0,
    matched: 0,
    missing: 0,
    datesPopulated: 0,
    datesUnchanged: 0,
    conflicts: 0,
    scoreDiscrepancies: 0,
    errors: 0,
    skipped: 0,
    review: 0,
  };

  for (const season of seasons) {
    database.exec("BEGIN;");

    try {
      const result = await collectSeason(database, season);
      database.exec("COMMIT;");
      for (const key of Object.keys(totals)) totals[key] += result[key];
      console.log(
        `${result.season}: discovered_games=${result.resultRecords}, mappings=${result.matched}, ` +
        `scheduled=${result.scheduled}, not_played=${result.notPlayed}, ` +
        `missing_match_id=${result.missing}, dates_populated=${result.datesPopulated}, ` +
        `dates_unchanged=${result.datesUnchanged}, conflicts=${result.conflicts}, score_discrepancies=${result.scoreDiscrepancies}, ` +
        `errors=${result.errors}, skipped=${result.skipped}, review=${result.review}`,
      );
    } catch (error) {
      database.exec("ROLLBACK;");
      throw error;
    }
  }

  console.log(
    `Game date collection totals: discovered_games=${totals.resultRecords}, mappings=${totals.matched}, ` +
    `scheduled=${totals.scheduled}, not_played=${totals.notPlayed}, ` +
    `missing_match_id=${totals.missing}, dates_populated=${totals.datesPopulated}, ` +
    `dates_unchanged=${totals.datesUnchanged}, conflicts=${totals.conflicts}, score_discrepancies=${totals.scoreDiscrepancies}, ` +
    `errors=${totals.errors}, skipped=${totals.skipped}, review=${totals.review}`,
  );
} finally {
  database.close();
}
