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
    pbp: [],
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
  assert.equal(failure.entity_key, "1|jsmith|");

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
  assert.equal(result.prepare("PRAGMA foreign_key_check").all().length, 0);
  result.close();
  console.log("PASS ambiguous LiveStats identity is retained as actionable review context");
} finally {
  try {
    rmSync(databasePath, { force: true });
    rmSync(tempDir, { recursive: true, force: true });
  } catch {
    // Windows may briefly retain SQLite file handles after the child exits.
  }
}