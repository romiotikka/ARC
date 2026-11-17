import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tempDir = mkdtempSync(join(tmpdir(), "arc-review-"));
const databasePath = join(tempDir, "arc.db");

try {
  const database = new DatabaseSync(databasePath);
  database.exec(readFileSync(join(root, "database", "schema.sql"), "utf8"));
  database.prepare("INSERT INTO leagues (league_id, name) VALUES (?, ?)").run(1, "Test League");
  database.prepare("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (?, ?, ?, ?)").run(20252026, "2025-26", 2025, 2026);
  database.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-review", 1, 20252026, "pending");
  database.prepare("INSERT INTO players (player_id, canonical_name, first_name, last_name) VALUES (?, ?, ?, ?)").run(1, "John Smith", "John", "Smith");
  database.prepare("INSERT INTO players (player_id, canonical_name, first_name, last_name) VALUES (?, ?, ?, ?)").run(2, "James Smith", "James", "Smith");

  const payload = {
    tm: {
      "1": { name: "Home", score: 0, pl: { a: { name: "J. Smith", shirtNumber: "7", playingPosition: "G" } } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [{ actionNumber: 1, period: 1, clock: "10:00", tno: "1", player: "J. Smith", actionType: "2pt", success: 1 }],
  };
  const jsonUrl = `data:application/json,${encodeURIComponent(JSON.stringify(payload))}`;
  database.prepare(
    "INSERT INTO source_livestats_games (game_id, league_id, season_id, provider_game_id, match_id, json_url) VALUES (?, ?, ?, ?, ?, ?)",
  ).run("game-review", 1, 20252026, "review-game", "review-1", jsonUrl);
  database.close();

  execFileSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "review-1"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "" },
    stdio: "pipe",
  });

  const result = new DatabaseSync(databasePath, { readOnly: true });
  const failure = result.prepare("SELECT * FROM failed_imports WHERE game_id = ?").get("game-review");
  assert.equal(failure.error_type, "identity_review");
  assert.equal(failure.provider, "fiba_livestats");
  assert.equal(failure.entity_type, "player_occurrence");
  assert.equal(failure.entity_key, "1|jsmith||7|a");

  const context = JSON.parse(failure.context_json);
  assert.equal(context.identity_reviews.length, 1);
  const review = context.identity_reviews[0];
  assert.equal(review.resolver_status, "review");
  assert.equal(review.raw_name, "J. Smith");
  assert.equal(review.team_id, 1);
  assert.equal(review.jersey_number, "7");
  assert.equal(review.position, "G");
  assert.deepEqual(review.candidate_players.map((candidate) => candidate.player_id), [1, 2]);
  assert.match(review.reason, /Ambiguous identity/);

  assert.equal(result.prepare("SELECT status FROM games WHERE game_id = ?").get("game-review").status, "boxscore_import_failed");
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM player_games").get().count, 0);
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM players").get().count, 2);
  const preservedSource = result.prepare(`SELECT s.snapshot_id,n.normalization_id,po.player_occurrence_id,d.status AS decision_set_status,
      id.status AS decision_status,id.evidence_json,po.original_name,po.jersey_number,po.provider_player_id
    FROM source_game_snapshots s
    JOIN source_snapshot_normalizations n ON n.snapshot_id=s.snapshot_id
    JOIN source_player_occurrences po ON po.normalization_id=n.normalization_id
    JOIN identity_decision_sets d ON d.normalization_id=n.normalization_id
    JOIN identity_decisions id ON id.decision_set_id=d.decision_set_id AND id.player_occurrence_id=po.player_occurrence_id
    WHERE s.game_id=?`).get("game-review");
  assert.equal(preservedSource.decision_set_status, "contains_review");
  assert.equal(preservedSource.decision_status, "review");
  assert.deepEqual(JSON.parse(preservedSource.evidence_json).resolverEvidence.candidate_players.map((candidate) => candidate.player_id), [1, 2]);
  assert.equal(preservedSource.original_name, "J. Smith");
  assert.equal(preservedSource.jersey_number, "7");
  assert.equal(preservedSource.provider_player_id, null);
  const preservedEvent = result.prepare("SELECT primary_player_occurrence_id,primary_identity_status FROM source_event_occurrences WHERE normalization_id=?").get(preservedSource.normalization_id);
  assert.equal(preservedEvent.primary_player_occurrence_id, preservedSource.player_occurrence_id);
  assert.equal(preservedEvent.primary_identity_status, "review");
  assert.equal(result.prepare("PRAGMA foreign_key_check").all().length, 0);
  result.close();

  const collisionDatabase = new DatabaseSync(databasePath);
  collisionDatabase.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-collision", 1, 20252026, "pending");
  const collisionPayload = {
    tm: {
      "1": { name: "Home", score: 0, pl: {
        a: { name: "K. Example", firstName: "Kristjan", familyName: "Example", shirtNumber: "33" },
        b: { name: "K. Example", firstName: "Kaspar", familyName: "Example", shirtNumber: "13" },
      } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [],
  };
  collisionDatabase.prepare(
    "INSERT INTO source_livestats_games (game_id, league_id, season_id, provider_game_id, match_id, json_url) VALUES (?, ?, ?, ?, ?, ?)",
  ).run("game-collision", 1, 20252026, "collision-game", "collision-1", `data:application/json,${encodeURIComponent(JSON.stringify(collisionPayload))}`);
  collisionDatabase.close();

  execFileSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "collision-1"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "" },
    stdio: "pipe",
  });
  const collisionResult = new DatabaseSync(databasePath, { readOnly: true });
  const collisionFailure = collisionResult.prepare("SELECT error_type, error_message, context_json FROM failed_imports WHERE game_id = ?").get("game-collision");
  assert.equal(collisionFailure.error_type, "identity_review", collisionFailure.error_message);
  const collisionReview = JSON.parse(collisionFailure.context_json).identity_reviews[0];
  assert.equal(collisionReview.resolver_status, "review");
  assert.match(collisionReview.reason, /Ambiguous identity/);
  assert.equal(collisionReview.raw_name, "K. Example");
  assert.equal(collisionReview.jersey_number, "13");
  assert.equal(collisionReview.candidate_players[0].canonical_name, "Kristjan Example");
  assert.equal(collisionResult.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id = ?").get("game-collision").count, 0);
  assert.equal(collisionResult.prepare("PRAGMA foreign_key_check").all().length, 0);
  collisionResult.close();

  const fullNameDatabase = new DatabaseSync(databasePath);
  fullNameDatabase.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-full-name", 1, 20252026, "pending");
  const fullNamePayload = {
    tm: {
      "1": { name: "Home", score: 2, pl: {
        a: { name: "J. Smith", firstName: "James", familyName: "Smith", shirtNumber: "7", playingPosition: "G" },
      } },
      "2": { name: "Away", score: 0, pl: {} },
    },
    pbp: [{ actionNumber: 1, period: 1, clock: "10:00", tno: "1", player: "J. Smith", actionType: "2pt", success: 1 }],
  };
  fullNameDatabase.prepare(
    "INSERT INTO source_livestats_games (game_id, league_id, season_id, provider_game_id, match_id, json_url) VALUES (?, ?, ?, ?, ?, ?)",
  ).run("game-full-name", 1, 20252026, "full-name-game", "full-name-1", `data:application/json,${encodeURIComponent(JSON.stringify(fullNamePayload))}`);
  fullNameDatabase.close();
  execFileSync(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--match-id", "full-name-1"], {
    env: { ...process.env, ARC_DATABASE_PATH: databasePath, ARC_IDENTITY_PROVIDERS: "" },
    stdio: "pipe",
  });
  const fullNameResult = new DatabaseSync(databasePath, { readOnly: true });
  const stagedFullName = fullNameResult.prepare("SELECT status,candidate_json FROM source_import_analytical_candidates WHERE game_id=?").get("game-full-name");
  assert.equal(stagedFullName.status, "reconstruction_failed");
  const stagedProjection = JSON.parse(stagedFullName.candidate_json).projection;
  assert.equal(stagedProjection.player_games[0].player_id, 2);
  assert.equal(stagedProjection.events[0].player_id, 2);
  assert.equal(fullNameResult.prepare("SELECT COUNT(*) AS count FROM player_games WHERE game_id=?").get("game-full-name").count, 0);
  assert.equal(fullNameResult.prepare("SELECT COUNT(*) AS count FROM events WHERE game_id=?").get("game-full-name").count, 0);
  assert.equal(fullNameResult.prepare("SELECT COUNT(*) AS count FROM players").get().count, 2);
  assert.equal(fullNameResult.prepare("PRAGMA foreign_key_check").all().length, 0);
  fullNameResult.close();
  console.log("PASS ambiguous LiveStats identity is retained as actionable review context");
} finally {
  try {
    rmSync(databasePath, { force: true });
    rmSync(tempDir, { recursive: true, force: true });
  } catch {
    // Windows may briefly retain SQLite file handles after the child exits.
  }
}
