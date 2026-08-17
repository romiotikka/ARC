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
  database.exec("CREATE TABLE failed_imports (status TEXT, retry_count INTEGER, failed_at TEXT)");
  database.prepare("INSERT INTO leagues (league_id, name) VALUES (?, ?)").run(1, "Test League");
  database.prepare("INSERT INTO seasons (season_id, label, start_year, end_year) VALUES (?, ?, ?, ?)").run(20252026, "2025-26", 2025, 2026);
  database.prepare("INSERT INTO games (game_id, league_id, season_id, status) VALUES (?, ?, ?, ?)").run("game-1", 1, 20252026, "pending");

  const payload = {
    tm: {
      "1": { name: "Home", score: 2, pl: { a: { name: "K. Kitsing", playerId: "live-42", shirtNumber: "7", playingPosition: "F", sPoints: 2, starter: 1 } } },
      "2": { name: "Away", score: 0, pl: { b: { name: "Other Player", playerId: "live-99", shirtNumber: "8", playingPosition: "G", sPoints: 0, starter: 1 } } },
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
