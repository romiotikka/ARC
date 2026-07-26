import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Optional, List, Dict

import requests

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "arc2.db"


def normalize_key(value: Optional[str]) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def is_initial_like(value: Optional[str]) -> bool:
    if not value:
        return True
    stripped = str(value).strip()
    if not stripped:
        return True
    if re.fullmatch(r"[A-ZÄÖÜŠŽa-zäöüšž](\.|\s*)", stripped):
        return True
    if re.fullmatch(r"([A-ZÄÖÜŠŽa-zäöüšž]\.?\s+)+", stripped):
        return True
    return False


def clean_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    return text


def ascii_variant(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    ascii_value = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(ch for ch in ascii_value if not unicodedata.combining(ch))
    return ascii_value if ascii_value and ascii_value != value else None


def better_name(candidate: Optional[str], current: Optional[str]) -> bool:
    if not candidate:
        return False
    if not current:
        return True
    candidate = str(candidate).strip()
    current = str(current).strip()
    if not candidate or not current:
        return bool(candidate)
    if is_initial_like(current) and not is_initial_like(candidate):
        return True
    if not is_initial_like(candidate) and is_initial_like(current):
        return False
    return len(candidate) > len(current)


def collect_variants(player_data: Dict) -> List[str]:
    variants: List[str] = []
    seen = set()

    def add(value: Optional[str]):
        clean = clean_name(value)
        if not clean:
            return
        if normalize_key(clean) in seen:
            return
        seen.add(normalize_key(clean))
        variants.append(clean)
        ascii_val = ascii_variant(clean)
        if ascii_val and normalize_key(ascii_val) not in seen:
            seen.add(normalize_key(ascii_val))
            variants.append(ascii_val)

    add(player_data.get("name"))
    add(player_data.get("scoreboardName"))
    add(player_data.get("internationalFirstName"))
    add(player_data.get("firstName"))
    add(player_data.get("internationalFamilyName"))
    add(player_data.get("familyName"))

    first = clean_name(player_data.get("internationalFirstName") or player_data.get("firstName"))
    last = clean_name(player_data.get("internationalFamilyName") or player_data.get("familyName"))
    if first and last:
        add(f"{first} {last}")
        add(f"{first[0]}. {last}")
        add(f"{first} {last[0]}.")

    return variants


def resolve_canonical_name(player_data: Dict, fallback: Optional[str]) -> Optional[str]:
    first = clean_name(player_data.get("internationalFirstName") or player_data.get("firstName"))
    last = clean_name(player_data.get("internationalFamilyName") or player_data.get("familyName"))
    if first and last and not is_initial_like(first) and not is_initial_like(last):
        return f"{first} {last}"

    short = clean_name(player_data.get("name") or player_data.get("scoreboardName"))
    if short and not is_initial_like(short) and len(short.split()) >= 2:
        return short
    return fallback


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    players = cur.execute("SELECT player_id, canonical_name FROM players ORDER BY CAST(player_id AS INTEGER)").fetchall()
    player_lookup = {}
    for row in players:
        player_lookup[normalize_key(row["canonical_name"])] = row["player_id"]

    alias_rows = cur.execute("SELECT player_id, alias_name FROM player_aliases").fetchall()
    for row in alias_rows:
        player_lookup.setdefault(normalize_key(row["alias_name"]), row["player_id"])

    player_game_rows = cur.execute("SELECT DISTINCT player_id, player_name FROM player_games WHERE player_id IS NOT NULL").fetchall()
    for row in player_game_rows:
        player_lookup.setdefault(normalize_key(row["player_name"]), row["player_id"])

    game_rows = cur.execute("SELECT game_id, json_url FROM source_livestats_games ORDER BY game_id").fetchall()
    processed = 0
    updated_players = 0
    updated_aliases = 0

    for row in game_rows:
        game_id = row["game_id"]
        json_url = row["json_url"]
        try:
            response = requests.get(json_url, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            print(f"skip {game_id}: {exc}")
            continue

        for team in payload.get("tm", {}).values():
            for player_data in team.get("pl", {}).values():
                short_name = clean_name(player_data.get("name") or player_data.get("scoreboardName"))
                player_id = None
                if short_name:
                    player_id = player_lookup.get(normalize_key(short_name))
                if not player_id:
                    for variant in collect_variants(player_data):
                        player_id = player_lookup.get(normalize_key(variant))
                        if player_id:
                            break
                if not player_id:
                    continue

                fallback_name = cur.execute("SELECT canonical_name FROM players WHERE player_id = ?", (player_id,)).fetchone()[0]
                canonical_name = resolve_canonical_name(player_data, fallback_name)
                current_name = cur.execute("SELECT canonical_name FROM players WHERE player_id = ?", (player_id,)).fetchone()[0]
                if canonical_name and better_name(canonical_name, current_name):
                    cur.execute("UPDATE players SET canonical_name = ? WHERE player_id = ?", (canonical_name, player_id))
                    updated_players += 1

                variants = collect_variants(player_data)
                for variant in variants:
                    if variant and normalize_key(variant) != normalize_key(canonical_name or ""):
                        cur.execute(
                            "INSERT OR IGNORE INTO player_aliases (player_id, alias_name, source) VALUES (?, ?, ?)",
                            (player_id, variant, "livestats"),
                        )
                        if cur.rowcount:
                            updated_aliases += 1

                # seed lookup for future use
                if canonical_name:
                    player_lookup[normalize_key(canonical_name)] = player_id
                for alias in variants:
                    player_lookup.setdefault(normalize_key(alias), player_id)

                processed += 1

        if processed % 50 == 0:
            conn.commit()
            print(f"processed {processed} player entries")

    conn.commit()
    conn.close()
    print(f"done: processed={processed}, updated_players={updated_players}, updated_aliases={updated_aliases}")


if __name__ == "__main__":
    main()
