#!/usr/bin/env node

import { DatabaseSync } from "node:sqlite";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { copyFile, mkdir, rename, writeFile } from "node:fs/promises";
import { existsSync, statSync } from "node:fs";
import { dirname, join, resolve, basename } from "node:path";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const productionDatabase = resolve(root, "data", "arc2.db");
const isDisposableRun = process.env.ARC_REBUILD_DISPOSABLE_TARGET === "1";
const targetPath = isDisposableRun && process.env.ARC_REBUILD_TEST_DATABASE
  ? resolve(process.env.ARC_REBUILD_TEST_DATABASE)
  : productionDatabase;
const prepareOnly = process.argv.includes("--prepare-only");
const timestamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
let backupPath = targetPath.replace(/\.db$/i, `.backup-${timestamp}.db`);
let candidatePath = targetPath.replace(/\.db$/i, `.candidate-${timestamp}.db`);
let sourceBackupHash = null;
const runLogDir = join(root, ".tmp-arc-rebuild", timestamp);
const python = process.env.ARC_PYTHON ?? process.env.PYTHON ?? (process.platform === "win32" ? "py" : "python3");
const CLEAR_TABLES = [
  "event_relationships", "active_reconstructions", "active_identity_decision_sets",
  "active_game_source_snapshots", "lineup_segments", "events", "player_games",
  "team_games", "source_import_analytical_candidates", "failed_imports",
];

function checkTarget() {
  if (!isDisposableRun && targetPath !== productionDatabase) {
    throw new Error(`Refusing unexpected production target: ${targetPath}`);
  }
  if (isDisposableRun && (!prepareOnly || targetPath === productionDatabase)) {
    throw new Error("Disposable target mode is allowed only for --prepare-only and must not target data/arc2.db.");
  }
  if (!existsSync(targetPath) || !statSync(targetPath).isFile()) {
    throw new Error(`The existing database is required for backup and baseline data: ${targetPath}`);
  }
}

async function confirmReset() {
  if (!stdin.isTTY || !stdout.isTTY) {
    throw new Error("Interactive terminal required. Run this task from VS Code's integrated terminal.");
  }
  console.log("\nWARNING: This task rebuilds the ARC database and replaces the active file only after candidate validation.");
  console.log(`Target: ${targetPath}`);
  console.log(`Candidate tables to clear: ${CLEAR_TABLES.join(", ")}`);
  console.log("Preserved: schema, leagues, seasons, teams, players, games, provider crosswalks, aliases and immutable source/reconstruction history.");
  console.log("A timestamped backup will be created first. Do not close VS Code while this task is running.");
  const terminal = createInterface({ input: stdin, output: stdout });
  try {
    const answer = await terminal.question(`Type REBUILD ${basename(targetPath)} to continue: `);
    if (answer !== `REBUILD ${basename(targetPath)}`) throw new Error("Confirmation did not match; no database changes were made.");
  } finally {
    terminal.close();
  }
}

function verifyDatabase(path) {
  const database = new DatabaseSync(path, { readOnly: true });
  try {
    const integrity = database.prepare("PRAGMA integrity_check").get().integrity_check;
    const foreignKeyViolations = database.prepare("PRAGMA foreign_key_check").all();
    if (integrity !== "ok") throw new Error(`SQLite integrity check failed for ${path}: ${integrity}`);
    if (foreignKeyViolations.length) throw new Error(`Foreign-key check failed for ${path}: ${foreignKeyViolations.length} violations`);
    return { integrity, foreignKeyViolations: foreignKeyViolations.length };
  } finally {
    database.close();
  }
}

function countRows(database, table) {
  return Number(database.prepare(`SELECT COUNT(*) AS n FROM "${table}"`).get().n);
}

function optionalCountRows(database, table) {
  const exists = database.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(table);
  return exists ? countRows(database, table) : 0;
}

async function prepareCandidate() {
  const source = new DatabaseSync(targetPath, { readOnly: true });
  let image;
  try {
    image = source.serialize();
  } finally {
    source.close();
  }
  sourceBackupHash = createHash("sha256").update(image).digest("hex");
  await writeFile(backupPath, image, { flag: "wx" });
  verifyDatabase(backupPath);
  await copyFile(backupPath, candidatePath);

  const candidate = new DatabaseSync(candidatePath);
  try {
    candidate.exec("PRAGMA foreign_keys = ON; BEGIN IMMEDIATE;");
    for (const table of CLEAR_TABLES) {
      if (candidate.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(table)) {
        candidate.exec(`DELETE FROM "${table}"`);
      }
    }
    candidate.exec(`
      UPDATE games
      SET status = 'scheduled_or_import_pending'
    `);
    candidate.exec("COMMIT;");
    return {
      mappings: countRows(candidate, "source_livestats_games"),
      games: countRows(candidate, "games"),
      teams: countRows(candidate, "teams"),
      players: countRows(candidate, "players"),
      immutableSnapshotsRetained: optionalCountRows(candidate, "source_game_snapshots"),
      immutableDecisionSetsRetained: optionalCountRows(candidate, "identity_decision_sets"),
    };
  } catch (error) {
    try { candidate.exec("ROLLBACK;"); } catch {}
    throw error;
  } finally {
    candidate.close();
  }
}

function verifyTargetStillMatchesBackup() {
  for (const suffix of ["-wal", "-shm"]) {
    if (existsSync(`${targetPath}${suffix}`)) {
      throw new Error(`Cannot safely replace the active database while SQLite sidecar ${targetPath}${suffix} exists. Close database users and resume after the sidecar is gone.`);
    }
  }
  const database = new DatabaseSync(targetPath);
  try {
    database.exec("BEGIN EXCLUSIVE;");
    const currentHash = createHash("sha256").update(database.serialize()).digest("hex");
    if (currentHash !== sourceBackupHash) {
      throw new Error("The active database changed after its backup was made; candidate retained and active file left untouched.");
    }
  } finally {
    try { database.exec("ROLLBACK;"); } catch {}
    database.close();
  }
}

function run(command, args, env, label) {
  return new Promise((resolvePromise, reject) => {
    console.log(`\n=== ${label} ===`);
    const child = spawn(command, args, { cwd: root, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let stdoutText = "";
    let stderrText = "";
    child.stdout.on("data", (chunk) => { const value = chunk.toString(); stdoutText += value; stdout.write(value); });
    child.stderr.on("data", (chunk) => { const value = chunk.toString(); stderrText += value; process.stderr.write(value); });
    child.on("error", reject);
    child.on("close", (code) => code === 0
      ? resolvePromise({ stdout: stdoutText, stderr: stderrText })
      : reject(new Error(`${label} exited with code ${code}`)));
  });
}

function parseSummary(output, label, requiredFields) {
  const line = output.split(/\r?\n/).find((value) => value.startsWith(label));
  if (!line) throw new Error(`Missing required summary from ${label}`);
  const values = Object.fromEntries([...line.matchAll(/([a-z_]+)=(\d+)/g)].map((match) => [match[1], Number(match[2])]));
  for (const field of requiredFields) if (!(field in values)) throw new Error(`Summary ${label} did not report ${field}`);
  return values;
}

function markSuccessfullyRetriedFailures() {
  const database = new DatabaseSync(candidatePath);
  try {
    database.exec(`
      UPDATE failed_imports
      SET status='resolved', notes=COALESCE(notes || char(10), '') || 'Resolved after successful rebuild-candidate retry.',
          updated_at=CURRENT_TIMESTAMP
      WHERE status='pending'
        AND EXISTS (SELECT 1 FROM team_games t WHERE t.game_id=failed_imports.game_id GROUP BY t.game_id HAVING COUNT(*)=2)
        AND EXISTS (SELECT 1 FROM active_reconstructions r JOIN reconstruction_runs rr USING (reconstruction_run_id)
                    WHERE r.game_id=failed_imports.game_id AND rr.status='succeeded')
    `);
  } finally {
    database.close();
  }
}

function validateCandidate(minimumMappings, importSummary, collection) {
  const database = new DatabaseSync(candidatePath, { readOnly: true });
  try {
    const integrity = database.prepare("PRAGMA integrity_check").get().integrity_check;
    const fk = database.prepare("PRAGMA foreign_key_check").all();
    if (integrity !== "ok" || fk.length) throw new Error(`Candidate database integrity failed: integrity=${integrity}, foreign_key_violations=${fk.length}`);
    const perSeason = database.prepare("SELECT season_id, COUNT(*) AS n FROM source_livestats_games GROUP BY season_id ORDER BY season_id").all();
    const mappings = perSeason.reduce((sum, row) => sum + Number(row.n), 0);
    const expectedSeasons = [20212022, 20222023, 20232024, 20242025, 20252026];
    const foundSeasons = perSeason.map((row) => Number(row.season_id));
    if (mappings < minimumMappings || expectedSeasons.some((seasonId) => !foundSeasons.includes(seasonId))) {
      throw new Error(`Expected crosswalk coverage for all five seasons ${expectedSeasons.join(", ")} with at least ${minimumMappings} preserved mappings; found ${mappings} mappings for ${foundSeasons.join(", ")}. A season with no resolved mapping cannot be silently omitted.`);
    }
    const eligible = Number(database.prepare(`
      SELECT COUNT(*) AS n FROM source_livestats_games s JOIN games g USING (game_id)
      WHERE COALESCE(g.status, '') NOT IN ('scheduled', 'not_played')
    `).get().n);
    const imported = Number(database.prepare("SELECT COUNT(DISTINCT game_id) AS n FROM team_games").get().n);
    const incompleteTeamGames = Number(database.prepare("SELECT COUNT(*) AS n FROM (SELECT game_id, COUNT(*) AS n FROM team_games GROUP BY game_id HAVING n <> 2)").get().n);
    const missingSource = Number(database.prepare("SELECT COUNT(*) AS n FROM team_games t WHERE NOT EXISTS (SELECT 1 FROM active_game_source_snapshots s WHERE s.game_id=t.game_id)").get().n);
    const missingReconstruction = Number(database.prepare("SELECT COUNT(*) AS n FROM team_games t WHERE NOT EXISTS (SELECT 1 FROM active_reconstructions r WHERE r.game_id=t.game_id)").get().n);
    const missingIdentityDecisionSet = Number(database.prepare("SELECT COUNT(*) AS n FROM team_games t WHERE NOT EXISTS (SELECT 1 FROM active_identity_decision_sets i WHERE i.game_id=t.game_id)").get().n);
    const invalidLineups = Number(database.prepare(`
      SELECT COUNT(*) AS n FROM lineup_segments s JOIN lineups l USING (lineup_id)
      WHERE l.player_1=l.player_2 OR l.player_1=l.player_3 OR l.player_1=l.player_4 OR l.player_1=l.player_5
         OR l.player_2=l.player_3 OR l.player_2=l.player_4 OR l.player_2=l.player_5
         OR l.player_3=l.player_4 OR l.player_3=l.player_5 OR l.player_4=l.player_5
    `).get().n);
    const pendingReviews = Number(database.prepare("SELECT COUNT(*) AS n FROM failed_imports WHERE status='pending'").get().n);
    const unresolvedActiveDecisions = Number(database.prepare(`
      SELECT COUNT(*) AS n FROM active_identity_decision_sets a
      JOIN identity_decisions d USING (decision_set_id)
      WHERE d.status <> 'resolved'
    `).get().n);
    const mismatchedActiveReconstructions = Number(database.prepare(`
      SELECT COUNT(*) AS n FROM active_reconstructions a
      JOIN reconstruction_runs r USING (reconstruction_run_id)
      JOIN active_game_source_snapshots s USING (game_id)
      JOIN active_identity_decision_sets i USING (game_id)
      WHERE r.snapshot_id <> s.snapshot_id OR r.normalization_id <> s.normalization_id
         OR r.decision_set_id <> i.decision_set_id OR r.status <> 'succeeded'
    `).get().n);
    if (imported !== eligible) throw new Error(`Eligible games=${eligible}, imported games=${imported}; one or more games were silently skipped.`);
    if (incompleteTeamGames || missingSource || missingIdentityDecisionSet || missingReconstruction || invalidLineups || pendingReviews || unresolvedActiveDecisions || mismatchedActiveReconstructions) {
      throw new Error(`Candidate quality gate failed: incomplete_team_games=${incompleteTeamGames}, missing_active_source=${missingSource}, missing_active_decision_set=${missingIdentityDecisionSet}, missing_active_reconstruction=${missingReconstruction}, mismatched_active_reconstructions=${mismatchedActiveReconstructions}, unresolved_active_decisions=${unresolvedActiveDecisions}, invalid_active_lineups=${invalidLineups}, pending_reviews=${pendingReviews}`);
    }
    if (collection.errors || collection.conflicts || collection.missing_match_id || importSummary.failed || importSummary.lineup_failures) {
      throw new Error(`Candidate is partial and will not replace the active database: collection_errors=${collection.errors}, collection_conflicts=${collection.conflicts}, score_discrepancies=${collection.score_discrepancies ?? 0}, missing_match_id=${collection.missing_match_id}, import_failed=${importSummary.failed}, lineup_failures=${importSummary.lineup_failures}.`);
    }
    return {
      integrity,
      foreignKeyViolations: fk.length,
      mappingsBySeason: perSeason.map((row) => `${row.season_id}:${row.n}`).join(", "),
      importedGames: imported,
      events: countRows(database, "events"),
      playerGames: countRows(database, "player_games"),
      lineupSegments: countRows(database, "lineup_segments"),
      pendingReviews,
      unresolvedActiveDecisions,
    };
  } finally {
    database.close();
  }
}

async function main() {
  checkTarget();
  const resumeIndex = process.argv.indexOf("--resume-candidate");
  const resumeCandidate = resumeIndex >= 0 ? process.argv[resumeIndex + 1] : null;
  if (resumeCandidate) {
    if (isDisposableRun) throw new Error("Candidate resume is not available in disposable prepare-only mode.");
    candidatePath = resolve(resumeCandidate);
    const dataDir = resolve(root, "data");
    if (dirname(candidatePath) !== dataDir || !/^arc2\.candidate-[\w-]+\.db$/i.test(basename(candidatePath))) {
      throw new Error("Resume path must be an ARC rebuild candidate directly inside data/.");
    }
    backupPath = candidatePath.replace(".candidate-", ".backup-");
    if (!existsSync(backupPath)) throw new Error(`Matching verified backup is missing: ${backupPath}`);
    if (!existsSync(candidatePath)) throw new Error(`Candidate does not exist: ${candidatePath}`);
    verifyDatabase(backupPath);
    verifyDatabase(candidatePath);
    sourceBackupHash = createHash("sha256").update(await readFile(backupPath)).digest("hex");
    if (!stdin.isTTY || !stdout.isTTY) throw new Error("Interactive terminal required to resume a rebuild candidate.");
    const terminal = createInterface({ input: stdin, output: stdout });
    try {
      console.log(`\nResume failed-game processing in candidate: ${candidatePath}`);
      const answer = await terminal.question(`Type RESUME ${basename(candidatePath)} to continue: `);
      if (answer !== `RESUME ${basename(candidatePath)}`) throw new Error("Resume confirmation did not match.");
    } finally { terminal.close(); }
  } else {
    if (prepareOnly) {
      console.log(`Disposable preparation target: ${targetPath}`);
    } else {
      await confirmReset();
    }
  }
  await mkdir(dirname(targetPath), { recursive: true });
  await mkdir(runLogDir, { recursive: true });
  console.log(`\nVerified backup path: ${backupPath}`);
  console.log(`Candidate path: ${candidatePath}`);
  const baseline = resumeCandidate
    ? (() => {
        const database = new DatabaseSync(candidatePath, { readOnly: true });
        try { return { mappings: countRows(database, "source_livestats_games") }; }
        finally { database.close(); }
      })()
    : await prepareCandidate();
  if (resumeCandidate) {
    console.log(`Resume candidate baseline: mappings=${baseline.mappings}`);
  } else {
    console.log(`Preserved baseline: games=${baseline.games}, mappings=${baseline.mappings}, teams=${baseline.teams}, players=${baseline.players}, source_snapshots=${baseline.immutableSnapshotsRetained}, decision_sets=${baseline.immutableDecisionSetsRetained}`);
  }

  const env = {
    ...process.env,
    ARC_DATABASE_PATH: candidatePath,
    ARC_LOG_DIR: runLogDir,
    ARC_PYTHON: python,
  };
  await run(process.execPath, [join(root, "scripts", "import", "init_database.mjs")], env, "Initialize current ARC schema and baseline seeds");
  const pyArgs = ["py", "py.exe"].includes(basename(python).toLowerCase()) ? ["-3"] : [];
  await run(python, [...pyArgs, join(root, "scripts", "migration", "migrate_source_snapshots.py"), "--database", candidatePath], env, "Apply Phase 2 source-evidence migration");
  await run(python, [...pyArgs, join(root, "scripts", "migration", "migrate_phase3_analytics.py"), "--database", candidatePath], env, "Apply Phase 3 analytics migration");
  verifyDatabase(candidatePath);
  if (prepareOnly) {
    console.log("\nDisposable preparation validation complete. No collection, provider import, or production database operation was run.");
    return;
  }

  const collectionRun = await run(process.execPath, [join(root, "scripts", "import", "collect_estlatbl_seasons.mjs")], env, "Collect all configured EstLatBL seasons (2021–22 through 2025–26)");
  const collection = parseSummary(collectionRun.stdout, "Game date collection totals:", ["discovered_games", "mappings", "scheduled", "not_played", "missing_match_id", "conflicts", "score_discrepancies", "errors", "dates_populated", "dates_unchanged", "review"]);
  if (collection.mappings === 0) throw new Error("No provider crosswalk mappings were matched; candidate retained and not promoted.");
  const importRun = await run(process.execPath, [join(root, "scripts", "import", "import_livestats_games.mjs"), "--all"], env, "Import every eligible FIBA LiveStats mapping and reconstruct lineups");
  const importSummary = parseSummary(importRun.stdout, "Import complete", ["succeeded", "failed"]);
  const lineupLine = importRun.stdout.split(/\r?\n/).find((line) => line.startsWith("Lineup reconstruction failures:"));
  importSummary.lineup_failures = lineupLine ? Number(lineupLine.match(/(\d+)/)?.[1] ?? 0) : 0;
  markSuccessfullyRetriedFailures();
  const results = validateCandidate(baseline.mappings, importSummary, collection);

  // A failed replacement leaves both the previous active database and backup available.
  verifyTargetStillMatchesBackup();
  await rename(candidatePath, targetPath);
  console.log("\n=== Rebuild promoted ===");
  console.log(`Active database: ${targetPath}`);
  console.log(`Backup retained: ${backupPath}`);
  console.log(`Games discovered=${collection.discovered_games}, imported=${results.importedGames}, scheduled=${collection.scheduled}, not_played=${collection.not_played}, failed=${importSummary.failed}`);
  console.log(`Dates populated=${collection.dates_populated ?? 0}, unchanged=${collection.dates_unchanged ?? 0}, review=${collection.review ?? 0}`);
  console.log(`Official league / FIBA score discrepancies=${collection.score_discrepancies}; FIBA box-score values remain authoritative for ARC player/event analytics.`);
  console.log(`Seasons mappings: ${results.mappingsBySeason}`);
  console.log(`Events=${results.events}, player_games=${results.playerGames}, lineup_segments=${results.lineupSegments}`);
  console.log(`Integrity=${results.integrity}, foreign_key_violations=${results.foreignKeyViolations}, pending_reviews=${results.pendingReviews}, unresolved_active_decisions=${results.unresolvedActiveDecisions}`);
}

main().catch((error) => {
  console.error(`\nREBUILD NOT PROMOTED: ${error.message}`);
  console.error(`The active database was not replaced. Backup and any candidate created have been retained.`);
  if (existsSync(backupPath)) console.error(`Backup: ${backupPath}`);
  if (existsSync(candidatePath)) console.error(`Candidate: ${candidatePath}`);
  process.exitCode = 1;
});
