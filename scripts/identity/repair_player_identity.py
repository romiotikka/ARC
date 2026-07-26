from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def search_players(connection: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    sql = """
    SELECT DISTINCT
        p.player_id,
        p.canonical_name
    FROM players p
    LEFT JOIN player_aliases pa
        ON pa.player_id = p.player_id
    WHERE
        LOWER(p.canonical_name) LIKE LOWER(?)
        OR LOWER(pa.alias_name) LIKE LOWER(?)
    ORDER BY p.canonical_name
    """

    pattern = f"%{query}%"
    return connection.execute(sql, (pattern, pattern)).fetchall()
def get_aliases(connection: sqlite3.Connection, player_id: int):
    return connection.execute(
        """
        SELECT alias_name
        FROM player_aliases
        WHERE player_id = ?
        ORDER BY alias_name
        """,
        (player_id,),
    ).fetchall()


def get_player_games(connection: sqlite3.Connection, player_id: int):
    return connection.execute(
        """
        SELECT
            player_name,
            COUNT(*) AS games
        FROM player_games
        WHERE player_id = ?
        GROUP BY player_name
        ORDER BY games DESC
        """,
        (player_id,),
    ).fetchall()


def get_teams(connection: sqlite3.Connection, player_id: int):
    return connection.execute(
        """
        SELECT DISTINCT team_name
        FROM player_games
        WHERE player_id = ?
        ORDER BY team_name
        """,
        (player_id,),
    ).fetchall()


def get_event_count(connection: sqlite3.Connection, player_id: int):
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM events
        WHERE player_id = ?
           OR event_secondary_player_id = ?
        """,
        (player_id, player_id),
    ).fetchone()

    return row[0]


def show_player(connection: sqlite3.Connection, player):
    player_id = player["player_id"]

    print("\n" + "=" * 60)
    print(f"PLAYER ID: {player_id}")
    print(f"Canonical: {player['canonical_name']}")

    print("\nAliases")
    print("-" * 20)

    for alias in get_aliases(connection, player_id):
        print(alias["alias_name"])

    print("\nPlayer games")
    print("-" * 20)

    for row in get_player_games(connection, player_id):
        print(f"{row['player_name']:<30} {row['games']}")

    print("\nTeams")
    print("-" * 20)

    for row in get_teams(connection, player_id):
        print(row["team_name"])

    print("\nEvents")
    print("-" * 20)
    print(get_event_count(connection, player_id))

    print("=" * 60)



def print_player_summary(connection: sqlite3.Connection, player_id: int):
    aliases = get_aliases(connection, player_id)
    games = get_player_games(connection, player_id)
    teams = get_teams(connection, player_id)
    events = get_event_count(connection, player_id)

    print(f"ID {player_id}")
    print(f"Games      : {sum(r['games'] for r in games)}")
    print(f"Events     : {events}")
    print(f"Aliases    : {len(aliases)}")

    print("Teams")
    if teams:
        for t in teams:
            print(f"    {t['team_name']}")
    else:
        print("    -")

    print("Player names")
    if games:
        for g in games:
            print(f"    {g['player_name']} ({g['games']})")
    else:
        print("    -")


def review_duplicate_canonical_names(connection: sqlite3.Connection) -> None:
    rows = connection.execute("""
        SELECT canonical_name, COUNT(*) AS cnt
        FROM players
        GROUP BY canonical_name
        HAVING COUNT(*) > 1
        ORDER BY canonical_name
    """).fetchall()

    print("\n" + "=" * 60)
    print("DUPLICATE CANONICAL NAMES")
    print("=" * 60)

    if not rows:
        print("\nNo duplicate canonical names found.")
        input("\nPress Enter to continue...")
        return

    for i,row in enumerate(rows,1):
        print("\n"+"="*60)
        print(row["canonical_name"])
        print("="*60)
        ids=connection.execute(
            "SELECT player_id FROM players WHERE canonical_name=? ORDER BY player_id",
            (row["canonical_name"],)
        ).fetchall()
        for p in ids:
            print_player_summary(connection,p["player_id"])
            print("-"*60)
        print(f"Duplicate {i} of {len(rows)}")
        input("\nPress Enter for next duplicate...")
    print("\nReview complete.")
    input("\nPress Enter to continue...")


def print_header() -> None:
    print("\n" + "=" * 60)
    print("ARC PLAYER IDENTITY REPAIR TOOL")
    print("=" * 60)


def main() -> None:
    connection = connect()

    try:
        while True:
            print_header()
            print("1. Search player")
            print("2. Review duplicate canonical names")
            print("0. Exit")

            choice = input("\nChoice: ").strip()

            if choice == "1":
                query = input("\nSearch: ").strip()

                try:
                    results = search_players(connection, query)
                except Exception as exc:
                    print(f"\nERROR: {exc}")
                    input("\nPress Enter to continue...")
                    continue

                if not results:
                    print("\nNo players found.")
                    input("\nPress Enter to continue...")
                    continue

                print()

                for index, player in enumerate(results, start=1):
                    print(
                        f"{index}. {player['canonical_name']} "
                        f"(ID {player['player_id']})"
                    )

                selection = input("\nSelect player number: ").strip()

                if not selection.isdigit():
                    continue

                selection = int(selection)

                if selection < 1 or selection > len(results):
                    print("Invalid selection.")
                    input("\nPress Enter to continue...")
                    continue

                show_player(connection, results[selection - 1])

                input("\nPress Enter to continue...")
                continue

            if choice == "2":
                review_duplicate_canonical_names(connection)
                continue

            if choice == "0":
                break

            print("\nInvalid choice.")
            input("\nPress Enter to continue...")

    finally:
        connection.close()


if __name__ == "__main__":
    main()