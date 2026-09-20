/*
 * End-to-end smoke test for the production LiveStats importer identity path.
 * Run with ARC_PYTHON set to a Python interpreter that can run the resolver.
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tempDir = mkdtempSync(join(tmpdir(), "arc-import-"));
const databasePath = join(tempDir, "arc.db");

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
    env: { ...process.env, ARC_DATABASE_PATH: databasePath },
    stdio: "pipe",
  });

  const result = new DatabaseSync(databasePath, { readOnly: true });
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM players").get().count, 2);
  const players = result.prepare("SELECT player_id, canonical_name FROM players ORDER BY player_id").all();
  assert.deepEqual(players.map((row) => typeof row.player_id), ["number", "number"]);
  assert.equal(players[0].player_id, 1);
  assert.equal(players[0].canonical_name, "Kristjan Kitsing");
  const teams = result.prepare("SELECT team_id, canonical_name FROM teams ORDER BY team_id").all();
  assert.deepEqual(teams.map((row) => typeof row.team_id), ["number", "number"]);
  assert.equal(teams[0].team_id, 1);
  assert.equal(result.prepare("SELECT external_team_id FROM team_external_ids WHERE provider = ? AND team_id = ?").get("fiba_livestats", 1).external_team_id, "HOME");
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM player_games WHERE player_id IS NOT NULL").get().count, 2);
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM events WHERE player_id IS NULL").get().count, 1);
  assert.equal(result.prepare("SELECT COUNT(*) AS count FROM events WHERE player_id IS NOT NULL").get().count, 1);
  assert.equal(
    result.prepare("SELECT external_player_id FROM player_external_ids WHERE provider = ?").get("fiba_livestats").external_player_id,
    "live-42",
  );
  result.close();
  console.log("PASS import delegates LiveStats players to IdentityResolver and preserves playerless events");
} finally {
  rmSync(tempDir, { recursive: true, force: true });
}
