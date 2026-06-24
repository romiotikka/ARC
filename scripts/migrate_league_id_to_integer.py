"""
Migrate league_id from TEXT to INTEGER in arc2.db and arc.db.

This script:
1. Reads existing league_id values (expected to be numeric strings like "1")
2. Converts them to integers
3. Updates all references across tables
"""

from pathlib import Path
import sqlite3
import shutil


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"


def migrate_database(db_path):
    """Migrate league_id to INTEGER in the specified database."""
    if not db_path.exists():
        print(f"⚠ Database not found: {db_path}")
        return False

    # Create a backup
    backup_path = db_path.with_suffix(db_path.suffix + ".league_id_backup")
    shutil.copy2(db_path, backup_path)
    print(f"✓ Backup created: {backup_path}")

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = OFF;")
    cursor = connection.cursor()

    try:
        # Check if league_id column exists and its current type
        cursor.execute("PRAGMA table_info(leagues)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        if "league_id" not in columns:
            print(f"  ℹ No league_id column in leagues table")
            return True

        if columns["league_id"] == "INTEGER":
            print(f"  ✓ league_id is already INTEGER, no migration needed")
            return True

        print(f"  Converting league_id from {columns['league_id']} to INTEGER...")

        # 1. Get all current league_id values
        cursor.execute("SELECT DISTINCT league_id FROM leagues")
        existing_leagues = [row[0] for row in cursor.fetchall()]
        print(f"    Found {len(existing_leagues)} leagues: {existing_leagues}")

        # 2. Create a mapping of text -> integer
        league_id_map = {}
        for league_id_str in existing_leagues:
            if league_id_str is not None:
                try:
                    league_id_map[league_id_str] = int(league_id_str)
                except (ValueError, TypeError):
                    # If can't convert, use hash of string to create a unique integer
                    league_id_map[league_id_str] = hash(league_id_str) % (2**31)
        print(f"    Mapping: {league_id_map}")

        # 3. Create new leagues table with INTEGER league_id
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leagues_new (
                league_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                country TEXT,
                region TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 4. Copy data from old to new table
        for old_id, new_id in league_id_map.items():
            cursor.execute(
                "SELECT name, country, region, created_at FROM leagues WHERE league_id = ?",
                (old_id,),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "INSERT INTO leagues_new (league_id, name, country, region, created_at) VALUES (?, ?, ?, ?, ?)",
                    (new_id, row[0], row[1], row[2], row[3]),
                )

        # 5. Update games table
        for old_id, new_id in league_id_map.items():
            cursor.execute("UPDATE games SET league_id = ? WHERE league_id = ?", (new_id, old_id))
            count = cursor.rowcount
            if count > 0:
                print(f"    Updated {count} rows in games table")

        # 6. Update source_livestats_games table
        for old_id, new_id in league_id_map.items():
            cursor.execute(
                "UPDATE source_livestats_games SET league_id = ? WHERE league_id = ?",
                (new_id, old_id),
            )
            count = cursor.rowcount
            if count > 0:
                print(f"    Updated {count} rows in source_livestats_games table")

        # 7. Drop old leagues table and rename new one
        cursor.execute("DROP TABLE leagues")
        cursor.execute("ALTER TABLE leagues_new RENAME TO leagues")
        print("    ✓ Renamed leagues_new to leagues")

        connection.commit()
        print(f"✓ Migration completed for {db_path.name}")
        return True

    except Exception as error:
        connection.rollback()
        print(f"✗ Migration failed for {db_path.name}: {error}")
        return False

    finally:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.close()


def main():
    print("Migrating league_id to INTEGER in ARC databases...\n")

    databases = [
        DATA_DIR / "arc.db",
        DATA_DIR / "arc2.db",
    ]

    success_count = 0
    for db_path in databases:
        print(f"Processing {db_path.name}...")
        if migrate_database(db_path):
            success_count += 1
        print()

    print(f"Summary: {success_count}/{len(databases)} databases migrated successfully")


if __name__ == "__main__":
    main()
