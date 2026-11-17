/*
 * End-to-end smoke test for the production LiveStats importer identity path.
 * Run with ARC_PYTHON set to a Python interpreter that can run the resolver.
 */
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { gunzipSync } from "node:zlib";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tempDir = mkdtempSync(join(root, ".tmp-import-"));
const databasePath = join(tempDir, "arc.db");
let testFailure = null;

try {
  const database = new DatabaseSync(databasePath);
  database.exec(readFileSync(join(root, "database", "schema.sql"), "utf8"));
  database.prepare("INSERT INTO leagues (league_id, name) VALUES (?, ?)").run(1, "Test League");
  database.prepare("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (?, ?, ?, ?)").run(20252026, "2025-26", 2025, 2026);
  database.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-1", 1, 20252026, "pending");

  const payload = {
    tm: {
      "1": { name: "Home", code: "HOME", score: 2, pl: { a: { name: "K. Kitsing", scoreboardName: "K. Kitsing", firstName: "Kristjan", familyName: "Kitsing", playerId: "live-42", shirtNumber: "7", playingPosition: "F", sPoints: 2, starter: 1 } } },
      "2": { name: "Away", code: "AWAY", score: 0, pl: { b: { name: "Other Player", playerId: "live-99", shirtNumber: "8", playingPosition: "G", sPoints: 0, starter: 1 } } },
    },
    pbp: [
      { actionNumber: 1, period: 1, clock: "10:00", tno: "1", player: "K. Kitsing", actionType: "2pt", success: 1 },
      { actionNumber: 2, period: 1, clock: "09:59", actionType: "game", subType: "start" },
    ],
  };
  const jsonUrl = `data:application/json,${encodeURIComponent(JSON.stringify(payload))}`;
  database.prepare(
    "INSERT INTO source_livestats_games (game_id, league_id, season_id, provider_game_id, match_id, json_url) VALUES (?, ?, ?, ?, ?, ?)",
  ).run("game-1", 1, 20252026, "1", "1", jsonUrl);
  database.close();

  execFileSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--all"], {
    env: {
      ...process.env,
      ARC_DATABASE_PATH: databasePath,
      ARC_IDENTITY_PROVIDERS: "",
      ARC_API_SPORTS_MAPPINGS: JSON.stringify({
        leagues: { "1": "api-league-test" },
        seasons: { "20252026": "api-season-test" },
        teams: { HOME: "api-team-home-test", AWAY: "api-team-away-test" },
      }),
    },
    stdio: "pipe",
  });

  const result = new DatabaseSync(databasePath, { readOnly: true });
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM players").get().count, 0);
  const firstCandidate = result.prepare("SELECT status,candidate_json FROM source_import_analytical_candidates WHERE game_id=?").get("game-1");
  assert.equal(firstCandidate.status, "reconstruction_failed");
  const firstProjection = JSON.parse(firstCandidate.candidate_json).projection;
  assert.equal(firstProjection.player_games.length, 2);
  assert.equal(firstProjection.events.filter((row) => row.player_id !== null).length, 1);
  assert.equal(firstProjection.events.filter((row) => row.player_id === null).length, 1);
  assert.equal(JSON.stringify(JSON.parse(firstCandidate.candidate_json).identity_changes).includes("live-42"), true);
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM player_games WHERE player_id IS NOT NULL").get().count, 0);
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM events").get().count, 0);
  result.close();

  const orderingSetup = new DatabaseSync(databasePath);
  orderingSetup.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-source-order", 1, 20252026, "pending");
  const orderingPayload = {
    tm: payload.tm,
    pbp: [
      { actionNumber: 300, period: 2, clock: "09:59:00", tno: "1", player: "K. Kitsing", actionType: "2pt", success: 0 },
      { actionNumber: 193, period: 2, clock: "10:00:00", actionType: "period", subType: "start", success: 1 },
      { actionNumber: 192, period: 1, clock: "00:00:00", actionType: "period", subType: "end", success: 1 },
      { actionNumber: 227, period: 1, clock: "00:42:60", tno: "2", player: "Other Player", actionType: "turnover", subType: "badpass", success: 1 },
      { actionNumber: 183, period: 1, clock: "00:42:60", tno: "1", player: "K. Kitsing", actionType: "2pt", success: 1 },
      { actionNumber: 2, period: 1, clock: "10:00:00", actionType: "period", subType: "start", success: 1 },
      { actionNumber: 1, period: 1, clock: "10:00:00", actionType: "game", subType: "start", success: 1 },
    ],
  };
  orderingSetup.prepare(
    "INSERT INTO source_livestats_games (game_id, league_id, season_id, provider_game_id, match_id, json_url) VALUES (?, ?, ?, ?, ?, ?)",
  ).run("game-source-order", 1, 20252026, "source-order", "source-order", `data:application/json,${encodeURIComponent(JSON.stringify(orderingPayload))}`);
  orderingSetup.close();
  execFileSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "source-order"], {
    env: {
      ...process.env,
      ARC_DATABASE_PATH: databasePath,
      ARC_IDENTITY_PROVIDERS: "",
      ARC_API_SPORTS_MAPPINGS: JSON.stringify({
        leagues: { "1": "api-league-test" },
        seasons: { "20252026": "api-season-test" },
        teams: { HOME: "api-team-home-test", AWAY: "api-team-away-test" },
      }),
    },
    stdio: "pipe",
  });
  const orderingResult = new DatabaseSync(databasePath, { readOnly: true });
  const orderingCandidate = JSON.parse(orderingResult.prepare("SELECT candidate_json FROM source_import_analytical_candidates WHERE game_id=?").get("game-source-order").candidate_json).projection;
  const orderingGame = orderingCandidate.game;
  assert.equal(
    orderingCandidate.events.find((row) => row.action_number === 227).possession_team_id_after,
    orderingGame.home_team_id,
  );
  assert.equal(
    orderingCandidate.events.find((row) => row.action_number === 183).possession_team_id_after,
    orderingGame.away_team_id,
  );
  assert.equal(
    orderingCandidate.events.find((row) => row.action_number === 300).possession_team_id_after,
    null,
  );
  orderingResult.close();

  const duplicateSetup = new DatabaseSync(databasePath);
  duplicateSetup.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-duplicate-action", 1, 20252026, "pending");
  const duplicatePayload = {
    tm: payload.tm,
    pbp: [
      { actionNumber: 1, period: 1, clock: "10:00", tno: "1", player: "K. Kitsing", actionType: "2pt", success: 1 },
      { actionNumber: 1, period: 1, clock: "09:59", tno: "2", player: "Other Player", actionType: "turnover", success: 1 },
    ],
  };
  duplicateSetup.prepare(
    "INSERT INTO source_livestats_games (game_id, league_id, season_id, provider_game_id, match_id, json_url) VALUES (?, ?, ?, ?, ?, ?)",
  ).run("game-duplicate-action", 1, 20252026, "duplicate-action", "duplicate-action", `data:application/json,${encodeURIComponent(JSON.stringify(duplicatePayload))}`);
  duplicateSetup.close();
  const duplicateImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "duplicate-action"], {
    env: {
      ...process.env,
      ARC_DATABASE_PATH: databasePath,
      ARC_IDENTITY_PROVIDERS: "",
      ARC_API_SPORTS_MAPPINGS: "",
    },
    encoding: "utf8",
  });
  assert.equal(duplicateImport.status, 0, `${duplicateImport.stdout}\n${duplicateImport.stderr}`);
  assert.match(duplicateImport.stdout, /succeeded=0, failed=1/);
  const duplicateResult = new DatabaseSync(databasePath, { readOnly: true });
  assert.equal(duplicateResult.prepare("SELECT COUNT(*) AS count FROM events WHERE game_id = ?").get("game-duplicate-action").count, 0);
  assert.match(
    duplicateResult.prepare("SELECT error_message FROM failed_imports WHERE game_id = ?").get("game-duplicate-action").error_message,
    /Duplicate LiveStats actionNumber 1/,
  );
  duplicateResult.close();

  const unicodeDatabase = new DatabaseSync(databasePath);
  unicodeDatabase.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-unicode", 1, 20252026, "pending");
  const unicodePayload = {
    tm: {
      "1": { name: "Home", score: 2, pl: {
        a: { name: "\u041b\u0435\u0432 \u674e", firstName: "\u041b\u0435\u0432", familyName: "\u674e", shirtNumber: "7" },
        b: { name: "\u041c\u0430\u0439\u044f \u738b", firstName: "\u041c\u0430\u0439\u044f", familyName: "\u738b", shirtNumber: "8" },
      } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [
      { actionNumber: 1, period: 1, clock: "10:00", tno: "1", player: "\u041b\u0435\u0432 \u674e", actionType: "2pt", success: 1 },
      { actionNumber: 2, period: 1, clock: "09:59", tno: "1", player: "\u041c\u0430\u0439\u044f \u738b", actionType: "2pt", success: 0 },
    ],
  };
  unicodeDatabase.prepare(
    "INSERT INTO source_livestats_games (game_id, league_id, season_id, provider_game_id, match_id, json_url) VALUES (?, ?, ?, ?, ?, ?)",
  ).run("game-unicode", 1, 20252026, "unicode-game", "unicode-1", `data:application/json,${encodeURIComponent(JSON.stringify(unicodePayload))}`);
  unicodeDatabase.close();
  execFileSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "unicode-1"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_API_SPORTS_MAPPINGS: "" },
    stdio: "pipe",
  });
  const unicodeResult = new DatabaseSync(databasePath, { readOnly: true });
  const unicodeCandidate = JSON.parse(unicodeResult.prepare("SELECT candidate_json FROM source_import_analytical_candidates WHERE game_id=?").get("game-unicode").candidate_json);
  const unicodeProjection = unicodeCandidate.projection;
  const unicodePlayers = unicodeProjection.player_games.sort((a,b) => a.shirt_number.localeCompare(b.shirt_number)).map((row) => row.player_id);
  const unicodeEvents = unicodeProjection.events.sort((a,b) => a.action_number - b.action_number).map((row) => row.player_id);
  const candidatePlayerNames = new Map((unicodeCandidate.identity_changes.players ?? []).map((row) => [row.player_id, row.canonical_name]));
  const unicodeNames = unicodeProjection.player_games.sort((a,b) => a.shirt_number.localeCompare(b.shirt_number)).map((row) => ({ player_name: row.player_name, canonical_name: candidatePlayerNames.get(row.player_id) }));
  assert.equal(unicodePlayers.length, 2);
  assert.notEqual(unicodePlayers[0], unicodePlayers[1]);
  assert.deepEqual(unicodeEvents, unicodePlayers);
  assert.deepEqual(unicodeNames.map((row) => row.player_name), ["\u041b\u0435\u0432 \u674e", "\u041c\u0430\u0439\u044f \u738b"]);
  assert.deepEqual(unicodeNames.map((row) => row.canonical_name), ["\u041b\u0435\u0432 \u674e", "\u041c\u0430\u0439\u044f \u738b"]);
  assert.equal(unicodeResult.prepare("PRAGMA foreign_key_check").all().length, 0);
  unicodeResult.close();

  const atomicSetup = new DatabaseSync(databasePath);
  atomicSetup.prepare("INSERT INTO players (player_id,canonical_name,first_name,last_name,identity_status) VALUES (?,?,?,?,'verified')").run(10,"Alice Example","Alice","Example");
  atomicSetup.prepare("INSERT INTO players (player_id,canonical_name,first_name,last_name) VALUES (?,?,?,?)").run(11,"John Smith","John","Smith");
  atomicSetup.prepare("INSERT INTO players (player_id,canonical_name,first_name,last_name) VALUES (?,?,?,?)").run(12,"James Smith","James","Smith");
  atomicSetup.prepare("INSERT INTO player_aliases (player_id,alias_name,source) VALUES (?,?,?)").run(10,"Alice E.","manual");
  atomicSetup.prepare("INSERT INTO player_external_ids (player_id,provider,external_player_id) VALUES (?,?,?)").run(10,"fiba_livestats","live-alice");
  atomicSetup.prepare("INSERT INTO games (game_id,league_id,season_id,status) VALUES (?,?,?,?)").run("game-review-atomic",1,20252026,"pending");
  const reviewPayload = {
    tm: {
      "1": { name: "Home", score: 2, pl: {
        a: { name: "A. Example", firstName: "Alice", familyName: "Example", playerId: "live-alice", shirtNumber: "9", playingPosition: "G", sPoints: 2, starter: 1 },
        b: { name: "New Candidate", firstName: "New", familyName: "Candidate", shirtNumber: "6", playingPosition: "F", sPoints: 0, starter: 0 },
        c: { name: "J. Smith", shirtNumber: "7", playingPosition: "G", sPoints: 0, starter: 0 },
      } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [{ actionNumber: 1, period: 1, clock: "10:00", tno: "1", player: "A. Example", actionType: "2pt", success: 1 }],
  };
  atomicSetup.prepare(
    "INSERT INTO source_livestats_games (game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES (?,?,?,?,?,?)",
  ).run("game-review-atomic",1,20252026,"review-atomic","review-atomic",`data:application/json,${encodeURIComponent(JSON.stringify(reviewPayload))}`);
  atomicSetup.close();

  const failedImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "review-atomic"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(failedImport.status, 0, `${failedImport.stdout}\n${failedImport.stderr}`);
  assert.match(failedImport.stdout, /succeeded=0, failed=1/);
  const atomicResult = new DatabaseSync(databasePath, { readOnly: true });
  const failure = atomicResult.prepare("SELECT context_json FROM failed_imports WHERE game_id = ?").get("game-review-atomic");
  assert.ok(failure);
  assert.equal(atomicResult.prepare("SELECT COUNT(*) AS count FROM players WHERE canonical_name = ?").get("New Candidate").count, 0);
  assert.equal(atomicResult.prepare("SELECT COUNT(*) AS count FROM player_aliases WHERE player_id = ? AND alias_name = ?").get(10,"A. Example").count, 0);
  assert.equal(atomicResult.prepare("SELECT alias_name FROM player_aliases WHERE player_id = ?").get(10).alias_name, "Alice E.");
  assert.equal(atomicResult.prepare("SELECT external_player_id FROM player_external_ids WHERE player_id = ? AND provider = ?").get(10,"fiba_livestats").external_player_id, "live-alice");
  assert.equal(atomicResult.prepare("SELECT COUNT(*) AS count FROM team_games WHERE game_id = ?").get("game-review-atomic").count, 0);
  assert.equal(atomicResult.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id = ?").get("game-review-atomic").count, 0);
  assert.equal(atomicResult.prepare("SELECT COUNT(*) AS count FROM events WHERE game_id = ?").get("game-review-atomic").count, 0);
  assert.equal(atomicResult.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id = ?").get("game-1").count, 0);
  assert.equal(atomicResult.prepare("SELECT status FROM games WHERE game_id = ?").get("game-review-atomic").status, "boxscore_import_failed");
  assert.equal(atomicResult.prepare("PRAGMA foreign_key_check").all().length, 0);
  atomicResult.close();

  const collisionSetup = new DatabaseSync(databasePath);
  collisionSetup.prepare("INSERT INTO games (game_id,league_id,season_id,status) VALUES (?,?,?,?)").run("game-kitsing-collision",1,20252026,"pending");
  const kitsingPayload = {
    tm: {
      "1": { name: "Home", score: 0, pl: {
        kaspar: { name: "K. Kitsing", firstName: "Kaspar", familyName: "Kitsing", shirtNumber: "13", playingPosition: "SG" },
        kristjan: { name: "K. Kitsing", firstName: "Kristjan", familyName: "Kitsing", shirtNumber: "33", playingPosition: "PF" },
      } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [],
  };
  collisionSetup.prepare(
    "INSERT INTO source_livestats_games (game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES (?,?,?,?,?,?)",
  ).run("game-kitsing-collision",1,20252026,"collision-atomic","collision-atomic",`data:application/json,${encodeURIComponent(JSON.stringify(kitsingPayload))}`);
  collisionSetup.close();
  const collisionImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "collision-atomic"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(collisionImport.status, 0);
  assert.match(collisionImport.stdout, /succeeded=0, failed=1/);
  const collisionResult = new DatabaseSync(databasePath, { readOnly: true });
  const collisionFailure = collisionResult.prepare("SELECT context_json FROM failed_imports WHERE game_id = ?").get("game-kitsing-collision");
  const collisionReviews = JSON.parse(collisionFailure.context_json).identity_reviews;
  assert.ok(collisionReviews.length >= 1);
  assert.equal(collisionReviews[0].reason.includes("Multiple roster players share the same source name"), false);
  assert.equal(collisionReviews[0].raw_name, "K. Kitsing");
  assert.equal(collisionResult.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id = ?").get("game-kitsing-collision").count, 0);
  assert.equal(collisionResult.prepare("PRAGMA foreign_key_check").all().length, 0);
  collisionResult.close();

  const ambiguousSetup = new DatabaseSync(databasePath);
  ambiguousSetup.prepare("INSERT INTO games (game_id,league_id,season_id,status) VALUES (?,?,?,?)").run("game-kitsing-ambiguous",1,20252026,"pending");
  const ambiguousPayload = {
    tm: {
      "1": { name: "Home", score: 0, pl: {
        first: { name: "K. Kitsing", shirtNumber: "13", playingPosition: "SG" },
        second: { name: "K. Kitsing", shirtNumber: "33", playingPosition: "PF" },
      } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [],
  };
  ambiguousSetup.prepare(
    "INSERT INTO source_livestats_games (game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES (?,?,?,?,?,?)",
  ).run("game-kitsing-ambiguous",1,20252026,"ambiguous-kitsing","ambiguous-kitsing",`data:application/json,${encodeURIComponent(JSON.stringify(ambiguousPayload))}`);
  ambiguousSetup.close();
  const ambiguousImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "ambiguous-kitsing"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(ambiguousImport.status, 0);
  assert.match(ambiguousImport.stdout, /succeeded=0, failed=1/);
  const ambiguousResult = new DatabaseSync(databasePath, { readOnly: true });
  const ambiguousFailure = ambiguousResult.prepare("SELECT context_json FROM failed_imports WHERE game_id = ?").get("game-kitsing-ambiguous");
  const ambiguousReviews = JSON.parse(ambiguousFailure.context_json).identity_reviews;
  assert.equal(ambiguousReviews.length, 2);
  assert.deepEqual(ambiguousReviews.map((review) => review.jersey_number).sort(), ["13", "33"]);
  assert.ok(ambiguousReviews.every((review) => review.reason.includes("Duplicate LiveStats name")));
  assert.equal(ambiguousResult.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id = ?").get("game-kitsing-ambiguous").count, 0);
  assert.equal(ambiguousResult.prepare("PRAGMA foreign_key_check").all().length, 0);
  ambiguousResult.close();

  const occurrenceSetup = new DatabaseSync(databasePath);
  occurrenceSetup.prepare("INSERT INTO players (player_id,first_name,last_name,canonical_name,identity_status) VALUES (?,?,?,?,?)").run(125,"Arvan-Johhannes","Lippasaar","Arvan-Johhannes Lippasaar","verified");
  occurrenceSetup.prepare("INSERT INTO players (player_id,first_name,last_name,canonical_name,identity_status) VALUES (?,?,?,?,?)").run(216,"Arden-Joosep","Lippasaar","Arden-Joosep Lippasaar","verified");
  occurrenceSetup.prepare("INSERT INTO player_aliases (player_id,alias_name,source) VALUES (?,?,?)").run(125,"A. Lippasaar","test");
  occurrenceSetup.prepare("INSERT INTO player_aliases (player_id,alias_name,source) VALUES (?,?,?)").run(216,"A. Lippasaar","test");
  occurrenceSetup.prepare("INSERT INTO player_external_ids (player_id,provider,external_player_id) VALUES (?,?,?)").run(125,"fiba_livestats","live-125");
  occurrenceSetup.prepare("INSERT INTO player_external_ids (player_id,provider,external_player_id) VALUES (?,?,?)").run(216,"fiba_livestats","live-216");
  occurrenceSetup.prepare("INSERT INTO games (game_id,league_id,season_id,status) VALUES (?,?,?,?)").run("game-lippasaar-occurrences",1,20252026,"pending");
  const occurrencePayload = {
    tm: {
      "1": { name: "Home", score: 0, pl: {
        "1": { name: "A. Lippasaar", firstName: "Arvan-Johhannes", familyName: "Lippasaar", playerId: "live-125", pno: "1", shirtNumber: "13", starter: 0 },
        roster216: { name: "A. Lippasaar", firstName: "Arden-Joosep", familyName: "Lippasaar", playerId: "1", pno: "pno-216", shirtNumber: "1", starter: 0 },
      } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [
      { actionNumber: 1, period: 1, clock: "08:00:00", tno: "1", player: "A. Lippasaar", playerId: "1", pno: "pno-216", shirtNumber: "1", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 2, period: 1, clock: "07:00:00", tno: "1", player: "A. Lippasaar", playerId: "live-125", pno: "pno-125", shirtNumber: "13", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 3, period: 1, clock: "06:00:00", tno: "1", player: "A. Lippasaar", playerId: "live-125", pno: "pno-125", shirtNumber: "1", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 4, period: 1, clock: "05:00:00", tno: "1", player: "A. Lippasaar", playerId: "1", pno: "1", shirtNumber: "1", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 5, period: 1, clock: "04:00:00", tno: "1", player: "A. Lippasaar", playerId: "live-125", shirtNumber: "13", secondaryPlayer: "A. Lippasaar", secondaryPlayerId: "1", secondaryShirtNumber: "1", actionType: "foul", success: 1 },
    ],
  };
  occurrenceSetup.prepare(
    "INSERT INTO source_livestats_games (game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES (?,?,?,?,?,?)",
  ).run("game-lippasaar-occurrences",1,20252026,"lippasaar-occurrences","lippasaar-occurrences",`data:application/json,${encodeURIComponent(JSON.stringify(occurrencePayload))}`);
  occurrenceSetup.close();
  const occurrenceImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "lippasaar-occurrences"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(occurrenceImport.status, 0, `${occurrenceImport.stdout}\n${occurrenceImport.stderr}`);
  const occurrenceResult = new DatabaseSync(databasePath, { readOnly: true });
  const occurrenceProjection = JSON.parse(occurrenceResult.prepare("SELECT candidate_json FROM source_import_analytical_candidates WHERE game_id=?").get("game-lippasaar-occurrences").candidate_json).projection;
  const occurrenceEvents = occurrenceProjection.events.sort((a,b) => a.action_number - b.action_number);
  const playerByJersey = new Map(occurrenceProjection.player_games.map((row) => [row.shirt_number, row.player_id]));
  assert.equal(occurrenceEvents[0].player_id, 216);
  assert.equal(occurrenceEvents[1].player_id, 125);
  assert.equal(occurrenceEvents[2].player_id, null);
  assert.equal(occurrenceEvents[3].player_id, 216);
  assert.equal(occurrenceEvents[4].player_id, 125);
  assert.equal(occurrenceEvents[4].event_secondary_player_id, 216);
  assert.equal(playerByJersey.get("1"), 216);
  assert.equal(playerByJersey.get("13"), 125);
  for (const [index, expected] of [[0, { playerId: "1", pno: "pno-216", shirtNumber: "1", player: "A. Lippasaar", tno: "1" }], [1, { playerId: "live-125", pno: "pno-125", shirtNumber: "13", player: "A. Lippasaar", tno: "1" }]]) {
    const metadata = JSON.parse(occurrenceEvents[index].metadata_json);
    for (const [field, value] of Object.entries(expected)) assert.equal(metadata[field], value);
    assert.deepEqual(metadata.arcIdentityResolution, { primary: "resolved", secondary: "not_applicable" });
  }
  const conflictingMetadata = JSON.parse(occurrenceEvents[2].metadata_json);
  assert.equal(conflictingMetadata.playerId, "live-125");
  assert.equal(conflictingMetadata.shirtNumber, "1");
  assert.deepEqual(conflictingMetadata.arcIdentityResolution, { primary: "conflict", secondary: "not_applicable" });
  const collisionMetadata = JSON.parse(occurrenceEvents[3].metadata_json);
  assert.deepEqual(collisionMetadata.arcIdentityResolution, { primary: "resolved", secondary: "not_applicable" });
  const independentSecondaryMetadata = JSON.parse(occurrenceEvents[4].metadata_json);
  assert.deepEqual(independentSecondaryMetadata.arcIdentityResolution, { primary: "resolved", secondary: "resolved" });
  occurrenceResult.close();

  const endToEndSetup = new DatabaseSync(databasePath);
  endToEndSetup.prepare("INSERT INTO games (game_id,league_id,season_id,status) VALUES (?,?,?,?)").run("game-lippasaar-e2e",1,20252026,"pending");
  const homeStarterNames = ["Axel Birch", "Boris Cedar", "Cameron Dune", "Dorian Elm", "Elias Flint"];
  const awayStarterNames = ["Fiona Grove", "Gareth Holt", "Hugo Iverson", "Iris Jones", "Jonas Kline"];
  const homeStarters = homeStarterNames.map((name, index) => ({
    name,
    firstName: name.split(" ")[0],
    familyName: name.split(" ").slice(1).join(" "),
    playerId: `e2e-home-${index + 1}`,
    shirtNumber: String(index + 5),
    starter: 1,
  }));
  const awayStarters = awayStarterNames.map((name, index) => ({
    name,
    firstName: name.split(" ")[0],
    familyName: name.split(" ").slice(1).join(" "),
    playerId: `e2e-away-${index + 1}`,
    shirtNumber: String(index + 21),
    starter: 1,
  }));
  const lippasaar125 = { name: "A. Lippasaar", firstName: "Arvan-Johhannes", familyName: "Lippasaar", playerId: "live-125", pno: "pno-125", shirtNumber: "13", starter: 0 };
  const lippasaar216 = { name: "A. Lippasaar", firstName: "Arden-Joosep", familyName: "Lippasaar", playerId: "live-216", pno: "pno-216", shirtNumber: "1", starter: 0 };
  const endToEndPayload = {
    tm: {
      "1": { name: "E2E Home", score: 7, tot_sPoints: 7, tot_sFieldGoalsMade: 3, tot_sFieldGoalsAttempted: 5,
        tot_sPointsFastBreak: 2, pl: { ...Object.fromEntries(homeStarters.map((player, index) => [`home-${index + 1}`, player])),
          "home-1": { ...homeStarters[0], sPointsFastBreak: 2, sPointsInThePaint: 2, sPointsSecondChance: 1,
            sBlocksReceived: 1, sFoulsPersonal: 2, sFoulsOn: 3, captain: 1 }, lippasaar125, lippasaar216 },
        shot: [
          { r: 1, x: 30.25, y: 48.75, p: "home-1", pno: "home-1", tno: 1, per: 1, perType: "REGULAR", actionType: "2pt", actionNumber: 8, previousAction: "9", subType: "layup", player: "Axel Birch", shirtNumber: "5" },
          { r: 0, x: 22.5, y: 51.25, p: "home-1", pno: "home-1", tno: 1, per: 1, perType: "REGULAR", actionType: "2pt", actionNumber: 9, previousAction: "", subType: "jumpshot", player: "Axel Birch", shirtNumber: "5" },
          { r: 1, x: 12.25, y: 49.5, p: "home-1", pno: "home-1", tno: 1, per: 1, perType: "REGULAR", actionType: "3pt", actionNumber: 10, previousAction: "", subType: "jumpshot", player: "Axel Birch", shirtNumber: "5" },
          { r: 0, x: 8.5, y: 47.75, p: "home-1", pno: "home-1", tno: 1, per: 1, perType: "REGULAR", actionType: "3pt", actionNumber: 11, previousAction: "", subType: "jumpshot", player: "Axel Birch", shirtNumber: "5" },
          { r: 0, x: 28.5, y: 50, p: "home-1", pno: "home-1", tno: 1, per: 1, perType: "REGULAR", actionType: "2pt", actionNumber: 14, previousAction: "", subType: "layup", player: "Axel Birch", shirtNumber: "5" },
        ] },
      "2": { name: "E2E Away", score: 0, pl: Object.fromEntries(awayStarters.map((player, index) => [`away-${index + 1}`, player])) },
    },
    pbp: [
      { actionNumber: 1, period: 1, periodType: "REGULAR", clock: "10:00:00", actionType: "period", subType: "start", success: 1 },
      { actionNumber: 2, period: 1, periodType: "REGULAR", clock: "08:00:00", tno: "1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "substitution", subType: "out", success: 1 },
      { actionNumber: 3, period: 1, periodType: "REGULAR", clock: "08:00:00", tno: "1", player: "A. Lippasaar", playerId: "live-216", shirtNumber: "1", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 4, period: 1, periodType: "REGULAR", clock: "07:00:00", tno: "1", player: "A. Lippasaar", playerId: "live-216", shirtNumber: "1", actionType: "substitution", subType: "out", success: 1 },
      { actionNumber: 5, period: 1, periodType: "REGULAR", clock: "07:00:00", tno: "1", player: "A. Lippasaar", playerId: "live-125", shirtNumber: "13", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 6, period: 1, periodType: "REGULAR", clock: "06:00:00", tno: "1", player: "A. Lippasaar", playerId: "live-125", shirtNumber: "13", actionType: "substitution", subType: "out", success: 1 },
      { actionNumber: 7, period: 1, periodType: "REGULAR", clock: "06:00:00", tno: "1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 8, period: 1, periodType: "REGULAR", clock: "05:00:00", tno: "1", pno: "home-1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "2pt", subType: "layup", success: 1 },
      { actionNumber: 9, period: 1, periodType: "REGULAR", clock: "04:59:00", tno: "1", pno: "home-1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "2pt", subType: "jumpshot", success: 0 },
      { actionNumber: 10, period: 1, periodType: "REGULAR", clock: "04:58:00", tno: "1", pno: "home-1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "3pt", subType: "jumpshot", success: 1 },
      { actionNumber: 11, period: 1, periodType: "REGULAR", clock: "04:57:00", tno: "1", pno: "home-1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "3pt", subType: "jumpshot", success: 0 },
      { actionNumber: 12, period: 1, periodType: "REGULAR", clock: "04:56:00", tno: "1", pno: "home-1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "freethrow", success: 1 },
      { actionNumber: 13, period: 1, periodType: "REGULAR", clock: "04:55:00", tno: "1", pno: "home-1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "freethrow", success: 0 },
      { actionNumber: 14, period: 1, periodType: "REGULAR", clock: "04:54:00", tno: "1", pno: "home-1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "2pt", subType: "layup", success: 0 },
      { actionNumber: 15, period: 1, periodType: "REGULAR", clock: "04:54:00", tno: "2", pno: "away-1", player: "Fiona Grove", playerId: "e2e-away-1", shirtNumber: "21", actionType: "block", success: 1, previousAction: "14" },
      { actionNumber: 16, period: 1, periodType: "REGULAR", clock: "04:53:00", tno: "1", pno: "home-2", player: "Boris Cedar", playerId: "e2e-home-2", shirtNumber: "6", actionType: "assist", success: 1, previousAction: "8" },
      { actionNumber: 17, period: 1, periodType: "REGULAR", clock: "00:00:00", actionType: "period", subType: "end", success: 1 },
    ],
  };
  endToEndSetup.prepare(
    "INSERT INTO source_livestats_games (game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES (?,?,?,?,?,?)",
  ).run("game-lippasaar-e2e",1,20252026,"lippasaar-e2e","lippasaar-e2e",`data:application/json,${encodeURIComponent(JSON.stringify(endToEndPayload))}`);
  endToEndSetup.close();
  const endToEndImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "lippasaar-e2e"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_PYTHON: process.env.ARC_PYTHON ?? "python", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(endToEndImport.status, 0, `${endToEndImport.stdout}\n${endToEndImport.stderr}`);
  const endToEndResult = new DatabaseSync(databasePath, { readOnly: true });
  try {
    const lippasaarEventIds = endToEndResult.prepare("SELECT action_number,player_id,metadata_json FROM events WHERE game_id=? AND action_number BETWEEN 3 AND 6 ORDER BY action_number").all("game-lippasaar-e2e");
    assert.deepEqual(lippasaarEventIds.map((row) => row.player_id), [216,216,125,125]);
    assert.ok(lippasaarEventIds.every((row) => JSON.parse(row.metadata_json).arcIdentityResolution.primary === "resolved"));
    assert.ok(
      endToEndResult.prepare("SELECT COUNT(*) AS count FROM lineup_segments WHERE game_id=?").get("game-lippasaar-e2e").count > 0,
      `${JSON.stringify(endToEndResult.prepare("SELECT error_type,error_message FROM failed_imports WHERE game_id=?").all("game-lippasaar-e2e"))}\n${JSON.stringify(endToEndResult.prepare("SELECT player_id,player_name,shirt_number,starter FROM player_games WHERE game_id=?").all("game-lippasaar-e2e"))}\n${endToEndImport.stdout}\n${endToEndImport.stderr}`,
    );
    assert.equal(endToEndResult.prepare("SELECT COUNT(*) AS count FROM failed_imports WHERE game_id=? AND error_type LIKE '%identity%'").get("game-lippasaar-e2e").count, 0);
    const snapshot = endToEndResult.prepare(`SELECT s.captured_body_sha256,s.payload_gzip,
      n.normalized_sha256,n.normalized_json_gzip,n.status
      FROM active_game_source_snapshots a
      JOIN source_game_snapshots s ON s.snapshot_id=a.snapshot_id
      JOIN source_snapshot_normalizations n ON n.normalization_id=a.normalization_id
      WHERE a.game_id=?`).get("game-lippasaar-e2e");
    assert.ok(snapshot?.captured_body_sha256);
    assert.ok(snapshot?.normalized_sha256);
    assert.equal(snapshot.status, "complete");
    const capturedBody = gunzipSync(snapshot.payload_gzip);
    assert.equal(createHash("sha256").update(capturedBody).digest("hex"), snapshot.captured_body_sha256);
    const normalizedBody = gunzipSync(snapshot.normalized_json_gzip);
    assert.equal(createHash("sha256").update(normalizedBody).digest("hex"), snapshot.normalized_sha256);
    const rosterEvidence = endToEndResult.prepare(`SELECT provider_player_id,pno,jersey_number,roster_entry_key
      FROM source_player_occurrences WHERE normalization_id=(SELECT normalization_id FROM active_game_source_snapshots WHERE game_id=?)
      AND original_name='A. Lippasaar' ORDER BY jersey_number`).all("game-lippasaar-e2e");
    assert.deepEqual(rosterEvidence.map((row) => ({
      provider_player_id: row.provider_player_id, pno: row.pno,
      jersey_number: row.jersey_number, roster_entry_key: row.roster_entry_key,
    })), [
      { provider_player_id: "live-216", pno: "pno-216", jersey_number: "1", roster_entry_key: "lippasaar216" },
      { provider_player_id: "live-125", pno: "pno-125", jersey_number: "13", roster_entry_key: "lippasaar125" },
    ]);
    assert.equal(endToEndResult.prepare(`SELECT COUNT(*) AS count FROM identity_decisions id
      JOIN identity_decision_sets ds USING(decision_set_id) JOIN source_player_occurrences po
      ON po.player_occurrence_id=id.player_occurrence_id WHERE ds.status='complete'
      AND po.normalization_id=(SELECT normalization_id FROM active_game_source_snapshots WHERE game_id=?)`).get("game-lippasaar-e2e").count, 12);
    assert.deepEqual(endToEndResult.prepare(`SELECT player_id,shirt_number FROM player_games
      WHERE game_id=? AND player_name='A. Lippasaar' ORDER BY shirt_number`).all("game-lippasaar-e2e").map((row) => ({ player_id: row.player_id, shirt_number: row.shirt_number })), [
      { player_id: 216, shirt_number: "1" }, { player_id: 125, shirt_number: "13" },
    ]);
    assert.equal(endToEndResult.prepare("SELECT COUNT(*) AS count FROM reconstruction_runs WHERE game_id=? AND status='succeeded'").get("game-lippasaar-e2e").count, 1);
    assert.equal(endToEndResult.prepare("SELECT COUNT(*) AS count FROM active_reconstructions WHERE game_id=?").get("game-lippasaar-e2e").count, 1);
    assert.equal(endToEndResult.prepare("SELECT COUNT(*) AS count FROM events WHERE game_id=? AND source_event_occurrence_id IS NULL").get("game-lippasaar-e2e").count, 0);
    const shotRows = endToEndResult.prepare("SELECT action_number,event_result,points,x,y,coordinate_system,distance FROM events WHERE game_id=? AND action_number BETWEEN 8 AND 14 ORDER BY action_number").all("game-lippasaar-e2e");
    assert.deepEqual(shotRows.map((row) => [row.event_result, row.points]), [["made",2],["missed",0],["made",3],["missed",0],["made",1],["missed",0],["missed",0]]);
    assert.deepEqual(shotRows.slice(0, 4).map((row) => [row.x,row.y]), [[30.25,48.75],[22.5,51.25],[12.25,49.5],[8.5,47.75]]);
    assert.ok(shotRows.slice(0, 4).every((row) => row.coordinate_system === "fiba_livestats_shot_object_native" && row.distance === null));
    assert.ok(shotRows.slice(4, 6).every((row) => row.x === null && row.y === null));
    assert.equal(shotRows[6].x, 28.5);
    assert.equal(shotRows[6].y, 50);
    assert.deepEqual(endToEndResult.prepare("SELECT from_event.action_number AS from_action,to_event.action_number AS to_action,r.source_field FROM event_relationships r JOIN events from_event ON from_event.event_id=r.from_event_id JOIN events to_event ON to_event.event_id=r.to_event_id WHERE r.game_id=? ORDER BY from_event.action_number").all("game-lippasaar-e2e").map((row) => ({ ...row })), [
      { from_action: 8, to_action: 9, source_field: "shot.previousAction" },
      { from_action: 15, to_action: 14, source_field: "previousAction" },
      { from_action: 16, to_action: 8, source_field: "previousAction" },
    ]);
    assert.deepEqual({ ...endToEndResult.prepare("SELECT points_fast_break,points_in_paint,points_second_chance,blocks_received,personal_fouls,fouls_drawn,captain FROM player_games WHERE game_id=? AND player_name='Axel Birch'").get("game-lippasaar-e2e") }, {
      points_fast_break: 2, points_in_paint: 2, points_second_chance: 1, blocks_received: 1, personal_fouls: 2, fouls_drawn: 3, captain: 1,
    });
    assert.equal(endToEndResult.prepare("SELECT field_goals_made FROM team_games WHERE game_id=? AND is_home=1").get("game-lippasaar-e2e").field_goals_made, 3);
  } finally {
    endToEndResult.close();
  }
  const previousActive = new DatabaseSync(databasePath, { readOnly: true });
  const previousState = {
    source: previousActive.prepare("SELECT snapshot_id,normalization_id FROM active_game_source_snapshots WHERE game_id=?").get("game-lippasaar-e2e"),
    reconstruction: previousActive.prepare("SELECT reconstruction_run_id FROM active_reconstructions WHERE game_id=?").get("game-lippasaar-e2e"),
    teamGames: previousActive.prepare("SELECT COUNT(*) AS count FROM team_games WHERE game_id=?").get("game-lippasaar-e2e").count,
    playerGames: previousActive.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id=?").get("game-lippasaar-e2e").count,
    events: previousActive.prepare("SELECT COUNT(*) AS count FROM events WHERE game_id=?").get("game-lippasaar-e2e").count,
    relationships: previousActive.prepare("SELECT COUNT(*) AS count FROM event_relationships WHERE game_id=?").get("game-lippasaar-e2e").count,
    lineups: previousActive.prepare("SELECT COUNT(*) AS count FROM lineup_segments WHERE game_id=?").get("game-lippasaar-e2e").count,
  };
  previousActive.close();
  const failedCandidatePayload = {
    ...endToEndPayload,
    tm: {
      ...endToEndPayload.tm,
      "1": { ...endToEndPayload.tm["1"], shot: [] },
    },
    pbp: [
      { actionNumber: 1, period: 1, periodType: "REGULAR", clock: "10:00:00", actionType: "period", subType: "start", success: 1 },
      { actionNumber: 2, period: 1, periodType: "REGULAR", clock: "08:00:00", tno: "1", player: "Axel Birch", playerId: "e2e-home-1", shirtNumber: "5", actionType: "substitution", subType: "sideways", success: 1 },
      { actionNumber: 3, period: 1, periodType: "REGULAR", clock: "00:00:00", actionType: "period", subType: "end", success: 1 },
    ],
  };
  const failedCandidateSource = new DatabaseSync(databasePath);
  failedCandidateSource.prepare("UPDATE source_livestats_games SET json_url=? WHERE game_id=?").run(`data:application/json,${encodeURIComponent(JSON.stringify(failedCandidatePayload))}`, "game-lippasaar-e2e");
  failedCandidateSource.close();
  const failedCandidateImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "lippasaar-e2e", "--recapture-source"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_PYTHON: process.env.ARC_PYTHON ?? "python", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(failedCandidateImport.status, 0, `${failedCandidateImport.stdout}\n${failedCandidateImport.stderr}`);
  const failedCandidateResult = new DatabaseSync(databasePath, { readOnly: true });
  try {
    const failedCandidateState = failedCandidateResult.prepare("SELECT status,error_message FROM source_import_analytical_candidates WHERE game_id=? ORDER BY candidate_id DESC LIMIT 1").get("game-lippasaar-e2e");
    assert.equal(failedCandidateState.status, "reconstruction_failed", failedCandidateState.error_message);
    assert.deepEqual(failedCandidateResult.prepare("SELECT snapshot_id,normalization_id FROM active_game_source_snapshots WHERE game_id=?").get("game-lippasaar-e2e"), previousState.source);
    assert.deepEqual(failedCandidateResult.prepare("SELECT reconstruction_run_id FROM active_reconstructions WHERE game_id=?").get("game-lippasaar-e2e"), previousState.reconstruction);
    assert.equal(failedCandidateResult.prepare("SELECT COUNT(*) AS count FROM team_games WHERE game_id=?").get("game-lippasaar-e2e").count, previousState.teamGames);
    assert.equal(failedCandidateResult.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id=?").get("game-lippasaar-e2e").count, previousState.playerGames);
    assert.equal(failedCandidateResult.prepare("SELECT COUNT(*) AS count FROM events WHERE game_id=?").get("game-lippasaar-e2e").count, previousState.events);
    assert.equal(failedCandidateResult.prepare("SELECT COUNT(*) AS count FROM event_relationships WHERE game_id=?").get("game-lippasaar-e2e").count, previousState.relationships);
    assert.equal(failedCandidateResult.prepare("SELECT COUNT(*) AS count FROM lineup_segments WHERE game_id=?").get("game-lippasaar-e2e").count, previousState.lineups);
  } finally {
    failedCandidateResult.close();
  }
  const recoverySource = new DatabaseSync(databasePath);
  recoverySource.prepare("UPDATE source_livestats_games SET json_url=? WHERE game_id=?").run(`data:application/json,${encodeURIComponent(JSON.stringify(endToEndPayload))}`, "game-lippasaar-e2e");
  recoverySource.close();
  const recoveryImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "lippasaar-e2e", "--recapture-source"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_PYTHON: process.env.ARC_PYTHON ?? "python", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(recoveryImport.status, 0, `${recoveryImport.stdout}\n${recoveryImport.stderr}`);
  const recoveredState = new DatabaseSync(databasePath, { readOnly: true });
  assert.equal(recoveredState.prepare("SELECT status FROM source_import_analytical_candidates WHERE game_id=? ORDER BY candidate_id DESC LIMIT 1").get("game-lippasaar-e2e").status, "promoted");
  assert.notEqual(recoveredState.prepare("SELECT reconstruction_run_id FROM active_reconstructions WHERE game_id=?").get("game-lippasaar-e2e").reconstruction_run_id, previousState.reconstruction.reconstruction_run_id);
  assert.equal(recoveredState.prepare("SELECT COUNT(*) AS count FROM active_reconstructions WHERE game_id=?").get("game-lippasaar-e2e").count, 1);
  assert.equal(recoveredState.prepare("SELECT COUNT(*) AS count FROM event_relationships WHERE game_id=?").get("game-lippasaar-e2e").count, previousState.relationships);
  assert.deepEqual(recoveredState.prepare("SELECT player_id FROM events WHERE game_id=? AND action_number BETWEEN 3 AND 6 ORDER BY action_number").all("game-lippasaar-e2e").map((row) => row.player_id), [216,216,125,125]);
  recoveredState.close();
  const changedUrlSetup = new DatabaseSync(databasePath);
  changedUrlSetup.prepare("UPDATE source_livestats_games SET json_url=? WHERE game_id=?").run("mock://provider-response-no-longer-available", "game-lippasaar-e2e");
  changedUrlSetup.close();
  const offlineTrace = spawnSync(process.env.ARC_PYTHON ?? "python", [
    join(root, "scripts", "core", "lineup_reconstructor.py"), "--database", databasePath,
    "--trace", "--trace-game", "game-lippasaar-e2e",
  ], { encoding: "utf8", timeout: 15000, windowsHide: true });
  assert.equal(offlineTrace.status, 0, `${offlineTrace.stdout}\n${offlineTrace.stderr}`);
  assert.match(offlineTrace.stdout, /TRACE_ERROR|FIRST_INVALID_STATE|exact_explanation=/);
  const offlineReplay = spawnSync(process.env.ARC_PYTHON ?? "python", [
    join(root, "scripts", "core", "lineup_reconstructor.py"), "--database", databasePath,
    "--game-id", "game-lippasaar-e2e", "--force",
  ], { encoding: "utf8", timeout: 15000, windowsHide: true });
  assert.equal(offlineReplay.status, 0, `${offlineReplay.stdout}\n${offlineReplay.stderr}`);
  const replayCheck = new DatabaseSync(databasePath, { readOnly: true });
  assert.equal(replayCheck.prepare("SELECT COUNT(*) AS count FROM reconstruction_runs WHERE game_id=? AND status='succeeded'").get("game-lippasaar-e2e").count, 3);
  const runHashes = replayCheck.prepare("SELECT input_sha256,output_sha256 FROM reconstruction_runs WHERE game_id=? ORDER BY reconstruction_run_id").all("game-lippasaar-e2e");
  assert.equal(runHashes[1].input_sha256, runHashes[2].input_sha256);
  assert.equal(runHashes[1].output_sha256, runHashes[2].output_sha256);
  replayCheck.close();

  const unresolvedSetup = new DatabaseSync(databasePath);
  unresolvedSetup.prepare("INSERT INTO games (game_id,league_id,season_id,status) VALUES (?,?,?,?)").run("game-lippasaar-unresolved",1,20252026,"pending");
  const unresolvedPayload = {
    ...endToEndPayload,
    tm: {
      ...endToEndPayload.tm,
      "1": { ...endToEndPayload.tm["1"], shot: [] },
    },
    pbp: [
      { actionNumber: 1, period: 1, periodType: "REGULAR", clock: "10:00:00", actionType: "period", subType: "start", success: 1 },
      { actionNumber: 2, period: 1, periodType: "REGULAR", clock: "05:00:00", tno: "1", player: "Unlisted Mystery Player", actionType: "substitution", subType: "in", success: 1 },
      { actionNumber: 3, period: 1, periodType: "REGULAR", clock: "00:00:00", actionType: "period", subType: "end", success: 1 },
    ],
  };
  unresolvedSetup.prepare(
    "INSERT INTO source_livestats_games (game_id,league_id,season_id,provider_game_id,match_id,json_url) VALUES (?,?,?,?,?,?)",
  ).run("game-lippasaar-unresolved",1,20252026,"lippasaar-unresolved","lippasaar-unresolved",`data:application/json,${encodeURIComponent(JSON.stringify(unresolvedPayload))}`);
  unresolvedSetup.close();
  const unresolvedImport = spawnSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "lippasaar-unresolved"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "", ARC_PYTHON: process.env.ARC_PYTHON ?? "python", ARC_API_SPORTS_MAPPINGS: "" },
    encoding: "utf8",
  });
  assert.equal(unresolvedImport.status, 0, `${unresolvedImport.stdout}\n${unresolvedImport.stderr}`);
  const unresolvedResult = new DatabaseSync(databasePath, { readOnly: true });
  try {
    const unresolvedProjection = JSON.parse(unresolvedResult.prepare("SELECT candidate_json FROM source_import_analytical_candidates WHERE game_id=?").get("game-lippasaar-unresolved").candidate_json).projection;
    const persistedUnresolved = unresolvedProjection.events.find((row) => row.action_number === 2);
    assert.equal(persistedUnresolved.player_id, null);
    assert.equal(JSON.parse(persistedUnresolved.metadata_json).arcIdentityResolution.primary, "unresolved");
    const unresolvedFailure = unresolvedResult.prepare("SELECT error_type,error_message FROM failed_imports WHERE game_id=?").get("game-lippasaar-unresolved");
    assert.equal(unresolvedFailure.error_type, "unresolved_player_identity", unresolvedFailure.error_message);
  } finally {
    unresolvedResult.close();
  }
  console.log("PASS import delegates LiveStats players to IdentityResolver and preserves playerless events");
} catch (error) {
  testFailure = error;
  throw error;
} finally {
  try {
    rmSync(tempDir, { recursive: true, force: true });
  } catch (cleanupError) {
    if (testFailure) {
      console.error(`Cleanup also failed: ${cleanupError.message}`);
    } else {
      throw cleanupError;
    }
  }
}
