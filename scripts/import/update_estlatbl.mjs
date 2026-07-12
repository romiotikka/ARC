#!/usr/bin/env node

/**
 * EstLat Automatic Update Pipeline
 * 
 * Production-ready orchestrator for continuous EstLat data collection and import.
 * Designed for scheduled execution (daily via cron or Windows Task Scheduler).
 * 
 * Features:
 * - Incremental collection (recent seasons only by default)
 * - Incremental import (unimported games only)
 * - Dead Letter Queue for failed games
 * - File-based logging
 * - Idempotent execution (safe to run multiple times)
 * - Individual game error recovery (continues on failure)
 * 
 * Usage:
 *   node scripts/update_estlatbl.mjs                    # Default: recent 2 seasons, limit import to 5
 *   node scripts/update_estlatbl.mjs --all-seasons      # Collect all 4 seasons
 *   node scripts/update_estlatbl.mjs --import-all       # Import all pending games
 *   node scripts/update_estlatbl.mjs --recent-count 3 --import-limit 10  # Custom limits
 * 
 * Exit codes:
 *   0 - Success (some or all games processed)
 *   1 - Fatal error (collection/import script failed)
 *   2 - Setup error (database initialization failed)
 */

import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { Logger, initializeDatabase, getDatabasePath } from "./lib/estlatbl-utils.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));

class UpdateOrchestrator {
  constructor() {
    this.logger = new Logger("estlatbl-update.log");
    this.startTime = Date.now();
  }

  parseArgs(argv) {
    const args = {
      allSeasons: false,
      recentCount: 2,
      importAll: false,
      importLimit: 5,
    };

    for (let index = 2; index < argv.length; index += 1) {
      const arg = argv[index];

      if (arg === "--all-seasons") {
        args.allSeasons = true;
      } else if (arg === "--recent-count") {
        args.recentCount = Number(argv[index + 1]);
        index += 1;
      } else if (arg === "--import-all") {
        args.importAll = true;
      } else if (arg === "--import-limit") {
        args.importLimit = Number(argv[index + 1]);
        index += 1;
      }
    }

    return args;
  }

  runScript(scriptName, scriptArgs) {
    return new Promise((resolve, reject) => {
      const script = spawn("node", [join(__dirname, scriptName), ...scriptArgs], {
        stdio: ["ignore", "pipe", "pipe"],
      });

      let stdout = "";

      script.stdout.on("data", (data) => {
        stdout += data.toString();
        process.stdout.write(data);
      });

      script.stderr.on("data", (data) => {
        process.stderr.write(data);
      });

      script.on("close", (code) => {
        if (code !== 0) {
          reject(new Error(`${scriptName} exited with code ${code}`));
        } else {
          resolve(stdout);
        }
      });

      script.on("error", (err) => {
        reject(err);
      });
    });
  }

  async initializeDatabase() {
    try {
      await this.logger.info("Initializing database...");
      const databasePath = getDatabasePath();
      const database = initializeDatabase(databasePath);
      database.close();
      await this.logger.info("Database initialized successfully");
    } catch (error) {
      await this.logger.error(`Database initialization failed: ${error.message}`);
      throw error;
    }
  }

  async runCollection(args) {
    await this.logger.info("Step 1: Collecting game mappings from EstLat...");

    const collectArgs = [];
    if (!args.allSeasons) {
      collectArgs.push("--recent");
      if (args.recentCount !== 2) {
        collectArgs.push("--recent-count", String(args.recentCount));
      }
    }

    try {
      await this.runScript("collect_estlatbl_seasons.mjs", collectArgs);
      await this.logger.info("✓ Collection complete");
    } catch (error) {
      await this.logger.error(`✗ Collection failed: ${error.message}`);
      throw error;
    }
  }

  async runImport(args) {
    await this.logger.info("Step 2: Importing game boxscores from FIBA LiveStats...");

    const importArgs = [];
    if (args.importAll) {
      importArgs.push("--all");
    } else {
      importArgs.push("--limit", String(args.importLimit));
    }

    try {
      await this.runScript("import_livestats_games.mjs", importArgs);
      await this.logger.info("✓ Import complete");
    } catch (error) {
      await this.logger.error(`✗ Import failed: ${error.message}`);
      throw error;
    }
  }

  getFailedImportStats() {
    try {
      const database = new DatabaseSync(getDatabasePath());
      const stats = database
        .prepare(
          `
        SELECT COUNT(*) as count FROM failed_imports
        WHERE status = 'pending'
        `,
        )
        .get();
      database.close();
      return stats.count;
    } catch {
      return null;
    }
  }

  async reportStatus(args) {
    const elapsed = Math.round((Date.now() - this.startTime) / 1000);
    const pendingFailures = this.getFailedImportStats();

    await this.logger.info("");
    await this.logger.info("=== Update Complete ===");
    await this.logger.info(`Duration: ${elapsed}s`);
    await this.logger.info(
      `Configuration: recent_seasons=${!args.allSeasons}, import_limit=${args.importLimit}`,
    );

    if (pendingFailures !== null && pendingFailures > 0) {
      await this.logger.warn(`⚠ ${pendingFailures} games in Dead Letter Queue (pending retry)`);
    }
  }

  async run(argv) {
    const args = this.parseArgs(argv);

    try {
      await this.logger.info("");
      await this.logger.info("╔═══════════════════════════════════════════════════════════╗");
      await this.logger.info("║    EstLat Automatic Update Pipeline                       ║");
      await this.logger.info("╚═══════════════════════════════════════════════════════════╝");
      await this.logger.info(`Started at ${new Date().toISOString()}`);

      // Initialize database and run migrations
      await this.initializeDatabase();

      // Run collection
      await this.runCollection(args);

      // Run import
      await this.runImport(args);

      // Report status
      await this.reportStatus(args);

      await this.logger.info("✓ Pipeline succeeded");
      process.exit(0);
    } catch (error) {
      await this.logger.error(`✗ Pipeline failed: ${error.message}`);
      process.exit(1);
    }
  }
}

const orchestrator = new UpdateOrchestrator();
orchestrator.run(process.argv).catch((error) => {
  console.error(`Fatal error: ${error.message}`);
  process.exit(2);
});
