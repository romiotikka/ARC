"""
Populate game_date for all games by scraping EstLatBL live-stats pages.

The dates are available in DD.MM.YY format on the live-stats HTML pages.
"""

from pathlib import Path
import sqlite3
import urllib.request
import re
from datetime import datetime
import time

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
LIVE_STATS_URL = "https://www.estlatbl.com/et/tulemused/{}/live-stats"
GAME_DATE_PATTERN = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")


def parse_game_date_html(html: str) -> str | None:
    """Parse game date from HTML in DD.MM.YY format to YYYY-MM-DD."""
    match = GAME_DATE_PATTERN.search(html)
    if not match:
        return None

    day, month, year = match.groups()
    year = int(year)
    if year < 100:
        year += 2000

    return f"{year:04d}-{int(month):02d}-{int(day):02d}"


def scrape_game_date(provider_game_id: str) -> str | None:
    """Fetch and parse game date from live-stats page."""
    url = LIVE_STATS_URL.format(provider_game_id)
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            html = response.read().decode("utf-8", errors="replace")
        return parse_game_date_html(html)
    except Exception as exc:
        print(f"[WARN] Failed to fetch {provider_game_id}: {exc}")
        return None


def populate_game_dates(db_path):
    """Populate game_date for all games."""
    if not db_path.exists():
        print(f"[WARN] Database not found: {db_path}")
        return False

    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys = OFF;")
    cursor = connection.cursor()

    try:
        # Get games without dates along with their provider_game_id
        cursor.execute("""
            SELECT g.game_id, s.provider_game_id
            FROM games g
            LEFT JOIN source_livestats_games s ON g.game_id = s.game_id
            WHERE g.game_date IS NULL
            ORDER BY s.provider_game_id
        """)
        games = cursor.fetchall()
        print(f"[INFO] Found {len(games)} games without dates")

        if not games:
            print(f"[INFO] No games to update")
            return True

        updated = 0
        failed = 0

        for game_id, provider_game_id in games:
            if not provider_game_id:
                print(f"[WARN] No provider_game_id for {game_id}")
                failed += 1
                continue

            print(f"[INFO] Scraping {provider_game_id}...", end=" ")
            date = scrape_game_date(provider_game_id)

            if date:
                cursor.execute("UPDATE games SET game_date = ? WHERE game_id = ?", (date, game_id))
                print(f"-> {date}")
                updated += 1
            else:
                print("FAILED")
                failed += 1

            # Rate limiting to avoid overwhelming the server
            time.sleep(0.5)

        connection.commit()
        print(f"[OK] Updated {updated} games, {failed} failed")
        return updated > 0

    except Exception as error:
        connection.rollback()
        print(f"[ERROR] Population failed: {error}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.close()


# Run population
print("Populating game_date for all games...")
print()

databases = [
    DATA_DIR / "arc.db",
    DATA_DIR / "arc2.db",
]

for db_path in databases:
    print(f"Processing {db_path.name}...")
    if populate_game_dates(db_path):
        print(f"[OK] Completed for {db_path.name}")
    print()
