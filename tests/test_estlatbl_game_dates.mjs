import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { persistValidatedGameDate } from "../scripts/core/game_dates.mjs";
import { classifyEstlatblResultMetadataIssues, findExistingEstlatblMapping, validateEstlatblMapping, validateEstlatblResultMetadata } from "../scripts/leagues/estlatbl/matching.mjs";
import { parseEstlatblResultsPage } from "../scripts/leagues/estlatbl/results.mjs";

const rescheduledGameRow = `
<tr>
  <td onclick="doRedirectGame('https://www.estlatbl.com/et/tulemused/2023212182/#c')" class="dateAndTimeTd" nowrap="">
    <div class="dateAndTime">K 22.03.2023, 19:00</div><div class="arena">Rakvere Spordihall</div>
  </td>
  <td onclick="doRedirectGame('https://www.estlatbl.com/et/tulemused/2023212182/#c')" class="homeTeam">
    <span class="homeTeamNameDesktop">BC Tarvas</span><span class="homeTeamNameMobile">TRV</span>
  </td>
  <td onclick="doRedirectGame('https://www.estlatbl.com/et/tulemused/2023212182/#c')" class="center result">102:83</td>
  <td onclick="doRedirectGame('https://www.estlatbl.com/et/tulemused/2023212182/#c')" class="right awayTeam">
    <span class="visitorTeamNameDesktop">Valmiera Glass VIA</span><span class="visitorTeamNameMobile">VIA</span>
  </td>
</tr>`;

function page(rows) {
  return `<html><table class="table standings scheduleAndResults"><thead><tr><th>Kuupäev</th></tr></thead><tbody>${rows}</tbody></table></html>`;
}

function memoryDatabase() {
  const database = new DatabaseSync(":memory:");
  database.exec(`
    CREATE TABLE games (
      game_id TEXT PRIMARY KEY, season_id INTEGER, game_date TEXT,
      home_team_id INTEGER, away_team_id INTEGER, home_score INTEGER, away_score INTEGER
    );
    CREATE TABLE source_livestats_games (
      game_id TEXT, season_id INTEGER, provider_game_id TEXT, match_id TEXT
    );
    CREATE TABLE teams (team_id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL);
    CREATE TABLE team_aliases (team_id INTEGER, alias_name TEXT);
  `);
  return database;
}

test("parses the captured official row, keeping date, game ID, teams and score together", () => {
  const parsed = parseEstlatblResultsPage(page(rescheduledGameRow));
  assert.deepEqual(parsed.issues, []);
  assert.equal(parsed.records.length, 1);
  assert.deepEqual(parsed.records[0], {
    providerGameId: "2023212182",
    gameDate: "2023-03-22",
    displayedTime: "19:00",
    homeTeam: "BC Tarvas",
    awayTeam: "Valmiera Glass VIA",
    homeScore: 102,
    awayScore: 83,
    venue: "Rakvere Spordihall",
    completed: true,
    issues: [],
  });
});

test("does not treat a row without a numeric final score as completed", () => {
  const scheduledRow = rescheduledGameRow.replace("102:83", "-");
  const parsed = parseEstlatblResultsPage(page(scheduledRow));
  assert.equal(parsed.records[0].completed, false);
  assert.equal(parsed.records[0].gameDate, "2023-03-22");
});

test("classifies the official no-game marker separately from malformed results", () => {
  const cancelledRow = rescheduledGameRow
    .replace("K 22.03.2023, 19:00", "L 09.03.2024, ")
    .replace("Rakvere Spordihall", "*mängu ei toimu")
    .replace("102:83", "20:0");
  const parsed = parseEstlatblResultsPage(page(cancelledRow));
  assert.equal(parsed.records.length, 0);
  assert.equal(parsed.notPlayedRecords.length, 1);
  assert.equal(parsed.notPlayedRecords[0].providerGameId, "2023212182");
  assert.equal(parsed.notPlayedRecords[0].completed, false);
  assert.deepEqual(parsed.issues, []);
});

test("rejects rows with missing identity or result fields instead of partially matching them", () => {
  const missingGameId = rescheduledGameRow.replace(/doRedirectGame\('[^']+'\)/g, "doRedirectGame('/wrong-path')");
  assert.equal(parseEstlatblResultsPage(page(missingGameId)).records.length, 0);

  const missingTeam = rescheduledGameRow.replace('<span class="homeTeamNameDesktop">BC Tarvas</span>', "");
  assert.equal(parseEstlatblResultsPage(page(missingTeam)).records.length, 0);

  const missingScoreCell = rescheduledGameRow.replace(/<td onclick="[^"]+" class="center result">102:83<\/td>/, "");
  assert.equal(parseEstlatblResultsPage(page(missingScoreCell)).records.length, 0);
});

test("fails closed on a missing table, invalid date, or duplicate provider game ID", () => {
  assert.throws(() => parseEstlatblResultsPage("<html></html>"), /results table/);
  const invalidDate = rescheduledGameRow.replace("22.03.2023", "31.02.2023");
  assert.equal(parseEstlatblResultsPage(page(invalidDate)).records.length, 0);
  const duplicate = parseEstlatblResultsPage(page(rescheduledGameRow + rescheduledGameRow));
  assert.equal(duplicate.records.length, 0);
  assert.ok(duplicate.issues.some((issue) => issue.reasons.includes("duplicate_provider_game_id_in_results_page")));
});

test("checks mapping, season, teams and scores before date persistence", () => {
  const database = memoryDatabase();
  const season = { seasonId: 20222023 };
  const result = { homeTeam: "BC Tarvas", awayTeam: "Valmiera Glass VIA", homeScore: 102, awayScore: 83 };
  database.exec(`
    INSERT INTO teams VALUES (1, 'BC Tarvas');
    INSERT INTO teams VALUES (2, 'Valmiera Glass VIA');
    INSERT INTO games VALUES ('estlatbl_2023212182', 20222023, NULL, 1, 2, 102, 83);
    INSERT INTO source_livestats_games VALUES ('estlatbl_2023212182', 20222023, '2023212182', '2159999');
  `);
  assert.deepEqual(validateEstlatblMapping(database, season, "estlatbl_2023212182", "2023212182", "2159999"), []);
  assert.deepEqual(validateEstlatblResultMetadata(database, "estlatbl_2023212182", season, result), []);
  assert.ok(validateEstlatblMapping(database, { seasonId: 20232024 }, "estlatbl_2023212182", "2023212182", "2159999").includes("mapping_season_conflict"));
  assert.ok(validateEstlatblResultMetadata(database, "estlatbl_2023212182", season, { ...result, awayTeam: "Wrong Club" }).includes("away_team_conflict"));
  assert.ok(validateEstlatblResultMetadata(database, "estlatbl_2023212182", season, { ...result, homeScore: 99 }).includes("home_score_conflict"));
  const scoreOnlyIssues = validateEstlatblResultMetadata(database, "estlatbl_2023212182", season, { ...result, homeScore: 85 });
  const scoreOnly = classifyEstlatblResultMetadataIssues(scoreOnlyIssues, { allowKnownScoreDiscrepancy: true });
  assert.deepEqual(scoreOnly, { conflicts: [], scoreDiscrepancies: ["home_score_conflict"] });
  assert.deepEqual(classifyEstlatblResultMetadataIssues(scoreOnlyIssues), { conflicts: ["home_score_conflict"], scoreDiscrepancies: [] });
  const combined = classifyEstlatblResultMetadataIssues(validateEstlatblResultMetadata(database, "estlatbl_2023212182", season, { ...result, homeTeam: "Wrong Club", homeScore: 85 }), { allowKnownScoreDiscrepancy: true });
  assert.deepEqual(combined, { conflicts: ["home_team_conflict"], scoreDiscrepancies: ["home_score_conflict"] });
  database.exec("INSERT INTO source_livestats_games VALUES ('other_game', 20222023, '2023212182', 'other_match')");
  assert.ok(validateEstlatblMapping(database, season, "estlatbl_2023212182", "2023212182", "2159999").includes("duplicate_provider_or_match_mapping"));
  database.close();
});

test("uses the provider game ID crosswalk first and rejects conflicting mappings", () => {
  const database = memoryDatabase();
  const season = { seasonId: 20222023 };
  database.exec(`
    INSERT INTO source_livestats_games VALUES ('estlatbl_2023212053', 20222023, '2023212053', '2155561');
  `);
  assert.deepEqual(
    findExistingEstlatblMapping(database, "estlatbl_2023212053", "2023212053", season),
    { status: "matched", matchId: "2155561" },
  );
  assert.equal(
    findExistingEstlatblMapping(database, "estlatbl_2023212053", "different-provider-id", season).status,
    "conflict",
  );
  assert.equal(
    findExistingEstlatblMapping(database, "estlatbl_new-game", "new-game", season).status,
    "missing",
  );
  database.close();
});

test("persists a date idempotently and preserves conflicting or invalid dates", () => {
  const database = memoryDatabase();
  database.exec("INSERT INTO games (game_id, game_date) VALUES ('game-a', NULL), ('game-b', '2023-03-21')");
  assert.deepEqual(persistValidatedGameDate(database, "game-a", "2023-03-22"), { status: "updated" });
  assert.deepEqual(persistValidatedGameDate(database, "game-a", "2023-03-22"), { status: "unchanged" });
  assert.equal(persistValidatedGameDate(database, "game-b", "2023-03-22").status, "conflict");
  assert.equal(persistValidatedGameDate(database, "game-a", "2023-02-31").status, "invalid_date");
  assert.equal(persistValidatedGameDate(database, "missing", "2023-03-22").status, "game_not_found");
  assert.equal(database.prepare("SELECT game_date FROM games WHERE game_id='game-b'").get().game_date, "2023-03-21");
  database.close();
});
