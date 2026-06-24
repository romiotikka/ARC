import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promises as fs } from "node:fs";
import { DatabaseSync } from "node:sqlite";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = join(__dirname, "..", "..");
const logsDir = join(rootDir, "logs");

/**
 * Logger - File-based logging for EstLat update pipeline
 * Logs to logs/ directory with timestamp and severity
 */
export class Logger {
  constructor(filename = "estlatbl-update.log") {
    this.filename = filename;
    this.filepath = join(logsDir, filename);
  }

  async ensureLogsDir() {
    try {
      await fs.mkdir(logsDir, { recursive: true });
    } catch (error) {
      // Directory may already exist
    }
  }

  formatMessage(level, message) {
    const timestamp = new Date().toISOString();
    return `[${timestamp}] ${level.padEnd(5)} ${message}`;
  }

  async write(level, message) {
    await this.ensureLogsDir();
    const formatted = this.formatMessage(level, message);
    await fs.appendFile(this.filepath, formatted + "\n", "utf8");
    console.log(formatted);
  }

  async info(message) {
    await this.write("INFO", message);
  }

  async error(message) {
    await this.write("ERROR", message);
  }

  async warn(message) {
    await this.write("WARN", message);
  }

  async debug(message) {
    await this.write("DEBUG", message);
  }
}

/**
 * DeadLetterQueue - Manages failed import tracking
 * Records failures for analysis, monitoring, and retry
 */
export class DeadLetterQueue {
  constructor(database) {
    this.database = database;
  }

  recordFailure(mapping, errorType, errorMessage) {
    this.database
      .prepare(
        `
        INSERT INTO failed_imports (
          game_id,
          league_id,
          season_id,
          match_id,
          provider_game_id,
          error_type,
          error_message,
          retry_count,
          status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_id, match_id) DO UPDATE SET
          last_error_at = CURRENT_TIMESTAMP,
          retry_count = retry_count + 1,
          error_type = excluded.error_type,
          error_message = excluded.error_message,
          updated_at = CURRENT_TIMESTAMP
        `,
      )
      .run(
        mapping.game_id,
        mapping.league_id,
        mapping.season_id,
        mapping.match_id,
        mapping.provider_game_id,
        errorType,
        errorMessage,
        0,
        "pending",
      );
  }

  getPendingFailures(limit = 10) {
    return this.database
      .prepare(
        `
        SELECT *
        FROM failed_imports
        WHERE status = 'pending'
        ORDER BY retry_count ASC, failed_at ASC
        LIMIT ?
        `,
      )
      .all(limit);
  }

  getFailureStats() {
    return this.database
      .prepare(
        `
        SELECT
          status,
          COUNT(*) as count,
          MAX(retry_count) as max_retries,
          MIN(failed_at) as oldest_failure
        FROM failed_imports
        GROUP BY status
        `,
      )
      .all();
  }
}

/**
 * Utility to initialize database with migrations
 */
export function initializeDatabase(databasePath) {
  const database = new DatabaseSync(databasePath);
  database.exec("PRAGMA foreign_keys = ON;");

  try {
    // Create Dead Letter Queue table if it doesn't exist
    database.exec(`
      CREATE TABLE IF NOT EXISTS failed_imports (
          failed_import_id INTEGER PRIMARY KEY AUTOINCREMENT,
          game_id TEXT NOT NULL,
          league_id INTEGER NOT NULL,
          season_id INTEGER NOT NULL,
          match_id TEXT NOT NULL,
          provider_game_id TEXT,
          error_type TEXT NOT NULL,
          error_message TEXT NOT NULL,
          failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_error_at TEXT,
          retry_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending',
          notes TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (game_id) REFERENCES games(game_id),
          FOREIGN KEY (league_id) REFERENCES leagues(league_id),
          FOREIGN KEY (season_id) REFERENCES seasons(season_id),
          UNIQUE (game_id, match_id)
      );
      
      CREATE INDEX IF NOT EXISTS idx_failed_imports_status
          ON failed_imports (status);
      
      CREATE INDEX IF NOT EXISTS idx_failed_imports_failed_at
          ON failed_imports (failed_at);
      
      CREATE INDEX IF NOT EXISTS idx_failed_imports_retry_count
          ON failed_imports (retry_count);
    `);
    
    return database;
  } catch (error) {
    console.error(`Failed to initialize database: ${error.message}`);
    throw error;
  }
}

/**
 * Utility to get database path consistently
 */
export function getDatabasePath() {
  return join(rootDir, "data", "arc2.db");
}
