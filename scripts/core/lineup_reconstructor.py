from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import unicodedata
import urllib.request
import urllib.request
from collections import defaultdict
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATABASE_PATH = ROOT_DIR / "data" / "arc2.db"
FETCH_TIMEOUT_SECONDS = 30
RECONSTRUCTION_ALGORITHM_VERSION = "lineup-reconstructor-phase2-v1"

FAILURE_INVALID_SIZE = "INVALID_SIZE"
FAILURE_DUPLICATE_PLAYER = "DUPLICATE_PLAYER"
FAILURE_OUT_PLAYER_NOT_PRESENT = "OUT_PLAYER_NOT_PRESENT"
FAILURE_IN_PLAYER_ALREADY_PRESENT = "IN_PLAYER_ALREADY_PRESENT"
FAILURE_WRONG_TEAM_PLAYER = "WRONG_TEAM_PLAYER"
FAILURE_MISSING_STARTER = "MISSING_STARTER"
FAILURE_PERIOD_TRANSITION_ERROR = "PERIOD_TRANSITION_ERROR"
FAILURE_SUBSTITUTION_BATCH_ERROR = "SUBSTITUTION_BATCH_ERROR"
FAILURE_AMBIGUOUS_PLAYER_IDENTITY = "AMBIGUOUS_PLAYER_IDENTITY"
FAILURE_PLAYER_IDENTITY_CONFLICT = "PLAYER_IDENTITY_CONFLICT"
FAILURE_UNRESOLVED_PLAYER_IDENTITY = "UNRESOLVED_PLAYER_IDENTITY"
FAILURE_UNKNOWN = "UNKNOWN"
DEADBALL_EVENT_TYPES = {"timeout", "foul", "foulon", "freethrow", "jumpball", "jump_ball"}


class LineupTransitionError(ValueError):
    def __init__(self, code: str, message: str, context: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.context = context or {}


def event_identity_with_persisted_evidence(event: dict, metadata_json: str | None, role: str = "primary") -> tuple[dict, str | None]:
    """Use the imported provider evidence snapshot when the event row has one."""
    if not metadata_json:
        return dict(event), None
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, json.JSONDecodeError):
        return dict(event), None
    if not isinstance(metadata, dict):
        return dict(event), None
    enriched = dict(event)
    for field in ("player", "playerId", "pno", "shirtNumber", "tno"):
        if field in metadata:
            enriched[field] = metadata[field]
    resolution = metadata.get("arcIdentityResolution")
    status = resolution.get(role) if isinstance(resolution, dict) else None
    if status in {"ambiguous", "conflict", "unresolved"}:
        code = {
            "ambiguous": FAILURE_AMBIGUOUS_PLAYER_IDENTITY,
            "conflict": FAILURE_PLAYER_IDENTITY_CONFLICT,
            "unresolved": FAILURE_UNRESOLVED_PLAYER_IDENTITY,
        }[status]
        raise LineupTransitionError(code, f"{code}: persisted import identity status is {status} for action {event.get('actionNumber')}")
    return enriched, status


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def table_has_column(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def ensure_duration_seconds_column(connection: sqlite3.Connection) -> None:
    if table_has_column(connection, "lineup_segments", "duration_seconds"):
        return

    connection.execute("ALTER TABLE lineup_segments ADD COLUMN duration_seconds INTEGER")


def normalize_text(value: object) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text if text else None


def normalize_player_key(value: object) -> str:
    text = normalize_text(value) or ""
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    return "".join(character for character in decomposed if character.isalnum())


def natural_player_sort_key(player_id: str) -> tuple[int, int | str]:
    if player_id.isdigit():
        return (0, int(player_id))
    return (1, player_id)


def clock_to_seconds(clock: str | None) -> int:
    if not clock:
        return 0

    parts = clock.split(":")
    if len(parts) < 2:
        return 0

    minutes = int(parts[0])
    seconds = int(parts[1])
    return minutes * 60 + seconds


def clock_group_key(clock: str | None) -> tuple[int, int, int] | tuple[str]:
    """Normalize minute/second clocks without collapsing distinct subsecond values."""
    text = normalize_text(clock) or ""
    parts = text.split(":")
    if len(parts) not in (2, 3):
        return (text,)
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) == 3 else 0)
    except ValueError:
        return (text,)


def clock_duration_seconds(start_clock: str, end_clock: str) -> int:
    return clock_to_seconds(start_clock) - clock_to_seconds(end_clock)


def load_source_snapshot_data(connection: sqlite3.Connection, game_row: sqlite3.Row) -> dict:
    normalization_id = game_row["normalization_id"] if "normalization_id" in game_row.keys() else None
    if normalization_id is None or not game_row["normalized_json_gzip"]:
        # Unit tests use a mock-only URL and replace this function with a local fixture.
        # Real provider URLs are never used as a reconstruction fallback.
        if "json_url" in game_row.keys() and str(game_row["json_url"]).startswith("mock://"):
            return fetch_json(game_row["json_url"])
        raise LineupTransitionError(
            FAILURE_UNRESOLVED_PLAYER_IDENTITY,
            f"SOURCE_SNAPSHOT_REQUIRED: game {game_row['game_id']} has no complete persisted source snapshot; provider URLs are not reconstruction inputs",
        )
    raw = connection.execute(
        "SELECT payload_gzip, captured_body_sha256 FROM source_game_snapshots WHERE snapshot_id = ?",
        (game_row["snapshot_id"],),
    ).fetchone()
    if raw is None:
        raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"SOURCE_SNAPSHOT_MISSING: game {game_row['game_id']}")
    body = gzip.decompress(raw[0])
    if hashlib.sha256(body).hexdigest() != raw[1]:
        raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"SOURCE_SNAPSHOT_HASH_MISMATCH: game {game_row['game_id']}")
    normalized_json = gzip.decompress(game_row["normalized_json_gzip"]).decode("utf-8")
    if hashlib.sha256(normalized_json.encode("utf-8")).hexdigest() != game_row["normalized_sha256"]:
        raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"NORMALIZED_SNAPSHOT_HASH_MISMATCH: game {game_row['game_id']}")
    if game_row["decision_set_status"] != "complete":
        raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"IDENTITY_DECISION_SET_INCOMPLETE: game {game_row['game_id']}")
    counts = connection.execute(
        """SELECT (SELECT COUNT(*) FROM source_player_occurrences WHERE normalization_id = ?),
                  (SELECT COUNT(*) FROM identity_decisions WHERE decision_set_id = ?),
                  (SELECT COUNT(*) FROM identity_decisions WHERE decision_set_id = ? AND status <> 'resolved')""",
        (normalization_id, game_row["decision_set_id"], game_row["decision_set_id"]),
    ).fetchone()
    if counts[0] != counts[1] or counts[2] != 0:
        raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"IDENTITY_DECISION_SET_INCOMPLETE: game {game_row['game_id']}")
    return json.loads(normalized_json)


def fetch_json(url: str) -> dict:
    """Legacy diagnostic helper; the reconstruction pipeline does not call provider URLs."""
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def build_player_lookup(connection: sqlite3.Connection) -> dict[str, str]:
    """Compatibility placeholder; ARC identities require a game/team roster context."""
    return {}


def resolve_substitution_occurrence(
    connection: sqlite3.Connection,
    game_id: str,
    team_id: str,
    event: dict,
    team_roster: dict,
) -> str:
    """Resolve an event against its team/game roster; pno is provenance only."""
    team_number = str(event.get("tno") or "")
    roster = team_roster.get(team_number) or {}
    game_teams = connection.execute(
        "SELECT home_team_id, away_team_id FROM games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    if game_teams is None:
        raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"{FAILURE_UNRESOLVED_PLAYER_IDENTITY}: missing game context for action {event.get('actionNumber')}")
    expected_team = normalize_text(game_teams[0 if team_number == "1" else 1]) if team_number in {"1", "2"} else None
    if expected_team is None or expected_team != str(team_id):
        raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: provider team number {team_number!r} does not map to game team {team_id}")
    provider_team_id = normalize_text(roster.get("teamId") or roster.get("code"))
    if provider_team_id:
        mapped_team = connection.execute(
            "SELECT team_id FROM team_external_ids WHERE provider = 'fiba_livestats' AND external_team_id = ?",
            (provider_team_id,),
        ).fetchone()
        expected_external = connection.execute(
            "SELECT external_team_id FROM team_external_ids WHERE provider = 'fiba_livestats' AND team_id = ?",
            (team_id,),
        ).fetchone()
        if mapped_team is not None and str(mapped_team[0]) != str(team_id):
            raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: provider team ID {provider_team_id!r} belongs to another ARC team")
        if expected_external is not None and normalize_text(expected_external[0]) != provider_team_id:
            raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: provider team ID disagrees with game team {team_id}")
    players = roster.get("pl") or {}
    roster_items = list(players.items())
    provider_id = normalize_text(event.get("playerId"))
    jersey = normalize_text(event.get("shirtNumber"))
    event_name = normalize_text(event.get("player"))
    name_key = normalize_player_key(event_name) if event_name else ""

    def roster_name_keys(player: dict) -> set[str]:
        values = [player.get("name"), player.get("scoreboardName")]
        first = normalize_text(player.get("internationalFirstName") or player.get("firstName"))
        last = normalize_text(player.get("internationalFamilyName") or player.get("familyName"))
        if first and last:
            values.append(f"{first} {last}")
            values.append(f"{first[0]}. {last}")
            values.append(f"{last} {first}")
        for value in list(values):
            text = normalize_text(value)
            if text and "," in text:
                family, given = text.split(",", 1)
                if family.strip() and given.strip():
                    values.append(f"{given.strip()} {family.strip()}")
        keys = {normalize_player_key(value) for value in values if normalize_text(value)}
        for value in values:
            parts = (normalize_text(value) or "").split()
            if len(parts) >= 2 and len(parts[0].rstrip(".")) > 1:
                keys.add(normalize_player_key(f"{parts[0][0]}. {' '.join(parts[1:])}"))
        return keys

    evidence_sets = []
    name_matches = {entry_id for entry_id, player in roster_items if name_key and name_key in roster_name_keys(player)}
    if name_key and name_matches:
        evidence_sets.append(name_matches)
    if provider_id:
        evidence_sets.append({entry_id for entry_id, player in roster_items if provider_id == normalize_text(player.get("playerId"))})
    if jersey:
        evidence_sets.append({entry_id for entry_id, player in roster_items if jersey == normalize_text(player.get("shirtNumber"))})
    if not evidence_sets:
        raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"{FAILURE_UNRESOLVED_PLAYER_IDENTITY}: no provider roster occurrence matches action {event.get('actionNumber')}")
    if any(not matches for matches in evidence_sets):
        if provider_id and any(provider_id == normalize_text(player.get("playerId")) for other_team, other_roster in team_roster.items() if other_team != team_number for player in (other_roster.get("pl") or {}).values()):
            raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: provider player ID {provider_id!r} belongs to another team")
        # Evidence missing from this roster is unresolved unless another supplied signal
        # identifies a local occurrence, in which case it is a contradiction.
        if any(matches for matches in evidence_sets):
            raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: supplied event evidence contradicts the team roster at action {event.get('actionNumber')}")
        raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"{FAILURE_UNRESOLVED_PLAYER_IDENTITY}: no provider roster occurrence matches action {event.get('actionNumber')}")
    candidates = set.intersection(*evidence_sets)
    if not candidates:
        raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: identity evidence points to different roster occurrences at action {event.get('actionNumber')}")
    if len(candidates) > 1:
        raise LineupTransitionError(FAILURE_AMBIGUOUS_PLAYER_IDENTITY, f"{FAILURE_AMBIGUOUS_PLAYER_IDENTITY}: multiple roster occurrences match action {event.get('actionNumber')}")

    entry_id = next(iter(candidates))
    source_player = players[entry_id]
    resolved_name_keys = roster_name_keys(source_player)
    roster_jersey = normalize_text(source_player.get("shirtNumber"))
    roster_provider_id = normalize_text(source_player.get("playerId"))
    game_players = connection.execute(
        "SELECT player_id, player_name, provider_player_id, shirt_number FROM player_games WHERE game_id = ? AND team_id = ?",
        (game_id, team_id),
    ).fetchall()
    rows = list(game_players)
    if roster_provider_id is not None and any(row[2] is not None for row in rows):
        matched = [row for row in rows if normalize_text(row[2]) == roster_provider_id]
        if not matched:
            raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: provider roster ID disagrees with this game's ARC player mapping at action {event.get('actionNumber')}")
        rows = matched
    if roster_jersey is not None and any(row[3] is not None for row in rows):
        matched = [row for row in rows if normalize_text(row[3]) == roster_jersey]
        if not matched:
            raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: provider roster jersey disagrees with this game's ARC player mapping at action {event.get('actionNumber')}")
        rows = matched
    if resolved_name_keys:
        def arc_name_keys(player_id: str) -> set[str]:
            values = list(item[0] for item in connection.execute("SELECT canonical_name FROM players WHERE player_id = ?", (player_id,)))
            values.extend(item[0] for item in connection.execute("SELECT alias_name FROM player_aliases WHERE player_id = ?", (player_id,)))
            return {normalize_player_key(value) for value in values if normalize_text(value)}
        event_name_keys = {name_key} if name_key else set()
        name_ids = {
            str(row[0]) for row in rows
            if set.intersection(resolved_name_keys | event_name_keys, arc_name_keys(str(row[0])))
        }
        if name_ids:
            rows = [row for row in rows if str(row[0]) in name_ids]
        elif name_key or resolved_name_keys:
            raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: event name {event_name!r} contradicts mapped ARC identity at action {event.get('actionNumber')}")
    ids = {str(row[0]) for row in rows if row[0] is not None}
    if not ids:
        raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"{FAILURE_UNRESOLVED_PLAYER_IDENTITY}: provider occurrence has no ARC player-game mapping at action {event.get('actionNumber')}")
    if len(ids) > 1:
        raise LineupTransitionError(FAILURE_AMBIGUOUS_PLAYER_IDENTITY, f"{FAILURE_AMBIGUOUS_PLAYER_IDENTITY}: provider occurrence maps to multiple ARC players at action {event.get('actionNumber')}")
    return next(iter(ids))


def load_game_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url, snap.captured_body_sha256,
               snap.snapshot_id, n.normalization_id, n.normalizer_version, n.normalized_json_gzip, n.normalized_sha256,
               d.decision_set_id, d.status AS decision_set_status, d.decision_set_sha256
        FROM games g
        JOIN source_livestats_games s ON s.game_id = g.game_id
        JOIN active_game_source_snapshots a ON a.game_id = g.game_id
        JOIN source_game_snapshots snap ON snap.snapshot_id = a.snapshot_id
        JOIN source_snapshot_normalizations n ON n.normalization_id = a.normalization_id AND n.status = 'complete'
        JOIN active_identity_decision_sets ai ON ai.game_id = g.game_id AND ai.normalization_id = a.normalization_id
        JOIN identity_decision_sets d ON d.decision_set_id = ai.decision_set_id AND d.normalization_id = a.normalization_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM lineup_segments ls
            WHERE ls.game_id = g.game_id
        )
        ORDER BY g.game_id
        """
    ).fetchall()


def load_validation_game(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url, snap.captured_body_sha256,
               a.snapshot_id, a.normalization_id, n.normalizer_version, n.normalized_json_gzip, n.normalized_sha256,
               d.decision_set_id, d.status AS decision_set_status, d.decision_set_sha256
        FROM games g
        JOIN source_livestats_games s ON s.game_id = g.game_id
        JOIN active_game_source_snapshots a ON a.game_id = g.game_id
        JOIN source_game_snapshots snap ON snap.snapshot_id = a.snapshot_id
        JOIN source_snapshot_normalizations n ON n.normalization_id = a.normalization_id AND n.status = 'complete'
        JOIN active_identity_decision_sets ai ON ai.game_id = g.game_id AND ai.normalization_id = a.normalization_id
        JOIN identity_decision_sets d ON d.decision_set_id = ai.decision_set_id AND d.normalization_id = a.normalization_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM lineup_segments ls
            WHERE ls.game_id = g.game_id
        )
        ORDER BY g.game_id
        LIMIT 1
        """
    ).fetchone()


def load_game_row_by_id(
    connection: sqlite3.Connection,
    game_id: str,
    source_snapshot_id: int | None = None,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url, snap.captured_body_sha256,
               snap.snapshot_id, n.normalization_id, n.normalizer_version, n.normalized_json_gzip, n.normalized_sha256,
               d.decision_set_id, d.status AS decision_set_status, d.decision_set_sha256
        FROM games g
        JOIN source_livestats_games s ON s.game_id = g.game_id
        LEFT JOIN active_game_source_snapshots a ON a.game_id = g.game_id
        LEFT JOIN source_game_snapshots snap ON snap.snapshot_id = COALESCE(?, a.snapshot_id)
        LEFT JOIN source_snapshot_normalizations n ON n.snapshot_id = snap.snapshot_id AND n.game_id = g.game_id
        LEFT JOIN active_identity_decision_sets ai ON ai.game_id = g.game_id AND ai.normalization_id = n.normalization_id
        LEFT JOIN identity_decision_sets d ON d.decision_set_id = CASE WHEN ? IS NOT NULL THEN (
            SELECT candidate.decision_set_id FROM identity_decision_sets candidate
            WHERE candidate.normalization_id = n.normalization_id AND candidate.status = 'complete'
            ORDER BY candidate.decision_set_version DESC LIMIT 1
        ) ELSE ai.decision_set_id END AND d.normalization_id = n.normalization_id
        WHERE g.game_id = ?
        LIMIT 1
        """,
        (source_snapshot_id, source_snapshot_id, game_id),
    ).fetchone()


def extract_sorted_events(data: dict) -> list[tuple[int, dict]]:
    """Order by logical period and clock; action numbers break same-clock ties."""
    events = list(enumerate(data.get("pbp", []), start=1))
    seen_action_numbers: set[int] = set()
    for index, event in events:
        raw_action_number = event.get("actionNumber")
        if raw_action_number is None:
            continue
        try:
            action_number = int(raw_action_number)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid LiveStats actionNumber {raw_action_number!r} at source index {index}") from exc
        if action_number in seen_action_numbers:
            raise ValueError(f"Duplicate LiveStats actionNumber {action_number}")
        seen_action_numbers.add(action_number)

    def event_order(item: tuple[int, dict]) -> tuple:
        index, event = item
        clock_key = clock_group_key(event.get("clock"))
        if len(clock_key) == 3 and all(isinstance(part, int) for part in clock_key):
            clock_order = (0, -clock_key[0], -clock_key[1], -clock_key[2])
        else:
            clock_order = (1, -index, 0, 0)
        raw_action_number = event.get("actionNumber")
        action_order = (
            (0, int(raw_action_number))
            if raw_action_number is not None
            else (1, -index)
        )
        return logical_period_number(event), clock_order, action_order, -index

    events.sort(key=event_order)
    return events


def logical_period_number(event: dict) -> int:
    """Map source periodType/period pairs to unique lineup period numbers.

    LiveStats restarts the numeric period counter for overtime. Preserve regular
    periods 1-4 and expose OT1, OT2, ... as periods 5, 6, ... for derived state.
    """
    period = int(event.get("period") or 0)
    period_type = (normalize_text(event.get("periodType")) or "REGULAR").upper()
    if period_type in {"OVERTIME", "OT"} and period in {1, 2, 3, 4}:
        return 4 + period if period <= 2 else period
    return period


def group_period_events(events: list[tuple[int, dict]]) -> dict[int, list[tuple[int, dict]]]:
    grouped: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    for index, event in events:
        period = logical_period_number(event)
        grouped[period].append((index, event))
    return dict(sorted(grouped.items()))


def event_context_payload(index: int, event: dict, connection: sqlite3.Connection, game_id: str, player_lookup: dict[str, str], team_ids: dict[str, str], team_rosters: dict | None = None) -> dict:
    action_number = int(event.get("actionNumber") or index)
    period = logical_period_number(event)
    clock = normalize_text(event.get("clock")) or ""
    action_type = (normalize_text(event.get("actionType")) or "").lower()
    team_number = normalize_text(event.get("tno")) or ""
    team_id = team_ids.get(team_number)

    player_id = None

    event_row = connection.execute(
        """
        SELECT event_id, event_type, team_id, player_id, event_secondary_player_id, metadata_json
        FROM events
        WHERE game_id = ?
          AND action_number = ?
        LIMIT 1
        """,
        (game_id, action_number),
    ).fetchone()

    if event_row:
        event_id = int(event_row[0])
        event_type = normalize_text(event_row[1]) or action_type
        event_team_id = normalize_text(event_row[2]) or team_id
        event_player_id = normalize_text(event_row[3]) or player_id
        event_secondary_player_id = normalize_text(event_row[4])
        metadata_json = normalize_text(event_row[5])
        if event_type == "substitution":
            if event_team_id != team_id:
                raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: persisted event team disagrees with provider team at action {action_number}")
            if team_rosters is None or not team_id:
                raise LineupTransitionError(FAILURE_UNRESOLVED_PLAYER_IDENTITY, f"{FAILURE_UNRESOLVED_PLAYER_IDENTITY}: missing roster context at action {action_number}")
            identity_event, _ = event_identity_with_persisted_evidence(event, metadata_json)
            resolved = resolve_substitution_occurrence(connection, game_id, team_id, identity_event, team_rosters)
            if event_player_id is not None and event_player_id != resolved:
                raise LineupTransitionError(FAILURE_PLAYER_IDENTITY_CONFLICT, f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: persisted player_id disagrees with provider occurrence at action {action_number}")
            event_player_id = resolved
    else:
        event_id = None
        event_type = action_type
        event_team_id = team_id
        event_player_id = player_id
        event_secondary_player_id = None
        metadata_json = None

    return {
        "index": index,
        "event_id": event_id,
        "action_number": action_number,
        "period": period,
        "clock": clock,
        "event_type": event_type,
        "team_id": event_team_id,
        "team_number": team_number,
        "player_id": event_player_id,
        "secondary_player_id": event_secondary_player_id,
        "metadata_json": metadata_json,
        "sub_type": (normalize_text(event.get("subType")) or "").lower(),
        "success": int(event.get("success") or 0),
        "live_payload_json": json.dumps(event, ensure_ascii=True, sort_keys=True),
    }


def classify_debug_root_cause(failure: dict, prior_sub_count_for_team: int) -> str:
    root = failure.get("root_cause")
    reason = (failure.get("reason") or "").lower()
    event_type = failure.get("event_type")
    operation = failure.get("operation")
    lineup_after = failure.get("lineup_after") or []
    period = int(failure.get("period") or 0)
    previous_event = failure.get("previous_event") or ""

    if root == FAILURE_WRONG_TEAM_PLAYER:
        return "F"

    if root == FAILURE_MISSING_STARTER:
        return "D"

    if root == FAILURE_PERIOD_TRANSITION_ERROR:
        return "E"

    if root == FAILURE_SUBSTITUTION_BATCH_ERROR:
        return "G"

    if root == FAILURE_OUT_PLAYER_NOT_PRESENT:
        return "B"

    if root == FAILURE_IN_PLAYER_ALREADY_PRESENT:
        return "C"

    if root == FAILURE_INVALID_SIZE and event_type == "substitution":
        if operation and operation.startswith("REMOVE"):
            return "A"
        if operation and operation.startswith("ADD"):
            return "B"
        if len(lineup_after) < 5:
            return "A"
        if len(lineup_after) > 5:
            return "B"

    if period > 1 and "period=" in previous_event:
        previous_period_text = previous_event.split("period=")[1].split("|")[0]
        if previous_period_text and int(previous_period_text) != period:
            return "E"

    if prior_sub_count_for_team == 0 and event_type == "substitution":
        return "D"

    if root == FAILURE_UNKNOWN and "http error" in reason:
        return "I"

    if root == FAILURE_UNKNOWN:
        return "I"

    return "I"


def validate_lineup_state(
    lineup: list[str],
    team_id: str,
    allowed_players: set[str],
) -> tuple[bool, str, str]:
    if len(lineup) != 5:
        return False, FAILURE_INVALID_SIZE, f"lineup size is {len(lineup)}"

    if len(set(lineup)) != len(lineup):
        return False, FAILURE_DUPLICATE_PLAYER, "duplicate player found in lineup"

    wrong_team_players = [player_id for player_id in lineup if player_id not in allowed_players]
    if wrong_team_players:
        return (
            False,
            FAILURE_WRONG_TEAM_PLAYER,
            f"players not on team {team_id}: {','.join(sorted(set(wrong_team_players), key=natural_player_sort_key))}",
        )

    return True, "", ""


def event_summary(context: dict | None) -> str:
    if context is None:
        return "none"
    return (
        f"period={context['period']}"
        f"|clock={context['clock']}"
        f"|action_number={context['action_number']}"
        f"|event_id={context['event_id']}"
        f"|event_type={context['event_type']}"
        f"|team_id={context['team_id']}"
        f"|player_id={context['player_id']}"
        f"|secondary_player_id={context['secondary_player_id']}"
    )


def format_lineup(lineup: list[str] | set[str]) -> str:
    players = sorted((normalize_text(player_id) or "" for player_id in lineup if normalize_text(player_id)), key=natural_player_sort_key)
    return ",".join(players)


def load_substitution_rows_for_action(connection: sqlite3.Connection, game_id: str, action_number: int) -> list[dict]:
    rows = connection.execute(
        """
        SELECT event_id, event_type, team_id, player_id, event_secondary_player_id, action_number, metadata_json
        FROM events
        WHERE game_id = ?
          AND action_number = ?
          AND event_type = 'substitution'
        ORDER BY event_id
        """,
        (game_id, action_number),
    ).fetchall()
    return [
        {
            "event_id": int(row["event_id"]),
            "event_type": normalize_text(row["event_type"]),
            "team_id": normalize_text(row["team_id"]),
            "player_id": normalize_text(row["player_id"]),
            "event_secondary_player_id": normalize_text(row["event_secondary_player_id"]),
            "action_number": int(row["action_number"]),
            "metadata_json": normalize_text(row["metadata_json"]),
        }
        for row in rows
    ]


def classify_trace_invalid_state(
    lineup_before: list[str],
    event: dict,
    team_id: str,
    resolved_player_id: str | None,
    operation_performed: str,
    allowed_players: dict[str, set[str]],
    duplicate_processing: bool,
    same_clock_events_for_team: list[dict],
) -> tuple[str, str]:
    if len(lineup_before) != 5:
        return "A", "lineup already had invalid size before this substitution"

    sub_type = (normalize_text(event.get("subType")) or "").lower()

    if sub_type == "out" and resolved_player_id and resolved_player_id not in set(lineup_before):
        return "B", f"outgoing player {resolved_player_id} was not on court"

    if sub_type == "in" and resolved_player_id is None:
        return "C", "incoming player could not be resolved (NULL)"

    if sub_type == "in" and operation_performed.startswith("SKIP_IN"):
        return "D", "incoming substitution was skipped"

    if resolved_player_id and resolved_player_id not in allowed_players.get(team_id, set()):
        return "G", f"resolved player {resolved_player_id} does not belong to team {team_id}"

    if duplicate_processing:
        return "H", "same substitution event appears to be processed more than once"

    if len(same_clock_events_for_team) > 1:
        has_out = any((normalize_text(item.get("subType")) or "").lower() == "out" for item in same_clock_events_for_team)
        has_in = any((normalize_text(item.get("subType")) or "").lower() == "in" for item in same_clock_events_for_team)
        if has_out and has_in:
            return "E", "same-clock substitution batch likely split/ordered incorrectly"

    return "I", "invalid lineup produced by substitution path not matched by A-H"


def run_trace_mode(connection: sqlite3.Connection, trace_game_id: str) -> None:
    game_row = load_game_row_by_id(connection, trace_game_id)
    if game_row is None:
        print("TRACE_ERROR")
        print(f"message=game not found: {trace_game_id}")
        return

    player_lookup = build_player_lookup(connection)
    game_data = load_source_snapshot_data(connection, game_row)
    sorted_events = extract_sorted_events(game_data)

    home_team_id = normalize_text(game_row["home_team_id"]) or ""
    away_team_id = normalize_text(game_row["away_team_id"]) or ""
    team_ids = {"1": home_team_id, "2": away_team_id}

    allowed_players = {
        team_id: {str(row[0]) for row in connection.execute(
            "SELECT DISTINCT player_id FROM player_games WHERE game_id = ? AND team_id = ? AND player_id IS NOT NULL",
            (trace_game_id, team_id),
        )}
        for team_id in (home_team_id, away_team_id)
    }

    current_lineups: dict[str, list[str]] = {
        home_team_id: list(build_team_roster(game_data, "1", connection, player_lookup, trace_game_id, home_team_id)),
        away_team_id: list(build_team_roster(game_data, "2", connection, player_lookup, trace_game_id, away_team_id)),
    }

    for team_id in (home_team_id, away_team_id):
        if len(current_lineups[team_id]) != 5:
            print("FIRST_INVALID_STATE")
            print(f"game_id={trace_game_id}")
            print("period=0")
            print("clock=")
            print("action_number=0")
            print(f"lineup_before={format_lineup(current_lineups[team_id])}")
            print("operation_performed=starter_reconstruction")
            print(f"lineup_after={format_lineup(current_lineups[team_id])}")
            print("exact_explanation=starter reconstruction was already wrong")
            print("determination=F")
            return

    batches = diagnostic_substitution_batches(sorted_events, connection, trace_game_id, player_lookup, team_ids, game_data.get("tm") or {})
    processed_batches: set[tuple[int, tuple[int, int, int] | tuple[str], str]] = set()

    for index, event in sorted_events:
        if (normalize_text(event.get("actionType")) or "").lower() != "substitution":
            continue
        team_id = team_ids.get(normalize_text(event.get("tno")) or "")
        if not team_id:
            continue

        period = logical_period_number(event)
        clock = normalize_text(event.get("clock")) or ""
        batch_key = (period, clock_group_key(clock), team_id)
        if batch_key in processed_batches:
            continue
        processed_batches.add(batch_key)
        batch = batches[batch_key]
        lineup_before = set(current_lineups[team_id])
        try:
            lineup_after = apply_substitution_batch(
                lineup_before,
                batch,
                "home" if team_id == home_team_id else "away",
                period,
                clock,
                game_id=trace_game_id,
                allowed_players=allowed_players[team_id],
            )
            current_lineups[team_id] = sorted(lineup_after, key=natural_player_sort_key)
            operation = "APPLY_BATCH"
            failure = None
        except LineupTransitionError as exc:
            lineup_after = lineup_before
            operation = "REJECT_BATCH"
            failure = exc

        print("TRACE_BATCH")
        print(f"period={period}")
        print(f"clock={clock}")
        print(f"team_id={team_id}")
        print(f"action_numbers={[item['actionNumber'] for item in batch]}")
        print(f"lineup_before={format_lineup(lineup_before)}")
        print(f"operation_performed={operation}")
        print(f"lineup_after={format_lineup(lineup_after)}")
        print(f"lineup_size={len(lineup_after)}")
        print(f"batch={json.dumps(batch, ensure_ascii=True, sort_keys=True)}")
        for item in batch:
            player_id = normalize_text(item.get("player_id")) or ""
            sub_type = (normalize_text(item.get("subType")) or "").lower()
            action_number = int(item["actionNumber"])
            print("TRACE_EVENT")
            print(f"period={period}")
            print(f"clock={item['clock']}")
            print(f"action_number={action_number}")
            print("event_type=substitution")
            print(f"team_id={team_id}")
            print(f"player_id={player_id}")
            print("secondary_player_id=")
            print(f"lineup_before={format_lineup(lineup_before)}")
            print(f"operation_performed={operation}({[member['actionNumber'] for member in batch]})")
            print(f"lineup_after={format_lineup(lineup_after)}")
            print(f"lineup_size={len(lineup_after)}")
            print(f"raw_livestats_payload={item['live_payload_json']}")
            print(
                "raw_events_table_rows="
                + json.dumps(load_substitution_rows_for_action(connection, trace_game_id, action_number), ensure_ascii=True, sort_keys=True)
            )
            print(f"resolved_out_player={player_id if sub_type == 'out' else ''}")
            print(f"resolved_in_player={player_id if sub_type == 'in' else ''}")
        if failure is not None:
            print("FIRST_INVALID_STATE")
            print(f"game_id={trace_game_id}")
            print(f"period={period}")
            print(f"clock={clock}")
            print(f"action_number={batch[0]['actionNumber']}")
            print(f"lineup_before={format_lineup(lineup_before)}")
            print(f"operation_performed={operation}")
            print(f"lineup_after={format_lineup(lineup_after)}")
            print(f"exact_explanation={failure.context['reason']}")
            print(f"determination={failure.code}")
            return

    print("FIRST_INVALID_STATE")
    print(f"game_id={trace_game_id}")
    print("period=")
    print("clock=")
    print("action_number=")
    print("lineup_before=")
    print("operation_performed=")
    print("lineup_after=")
    print("exact_explanation=no invalid lineup state encountered")
    print("determination=I")


def debug_first_invalid_event(
    connection: sqlite3.Connection,
    game_row: sqlite3.Row,
    player_lookup: dict[str, str],
) -> dict | None:
    game_id = game_row["game_id"]
    game_data = load_source_snapshot_data(connection, game_row)
    sorted_events = extract_sorted_events(game_data)

    team_ids = {
        "1": normalize_text(game_row["home_team_id"]) or "",
        "2": normalize_text(game_row["away_team_id"]) or "",
    }

    allowed_players = {
        team_id: {str(row[0]) for row in connection.execute(
            "SELECT DISTINCT player_id FROM player_games WHERE game_id = ? AND team_id = ? AND player_id IS NOT NULL",
            (game_id, team_id),
        )}
        for team_id in (team_ids["1"], team_ids["2"])
    }

    current_lineups = {
        team_ids["1"]: list(build_team_roster(game_data, "1", connection, player_lookup, game_id, team_ids["1"])),
        team_ids["2"]: list(build_team_roster(game_data, "2", connection, player_lookup, game_id, team_ids["2"])),
    }
    batches = diagnostic_substitution_batches(sorted_events, connection, game_id, player_lookup, team_ids, game_data.get("tm") or {})
    processed_batches: set[tuple[int, tuple[int, int, int] | tuple[str], str]] = set()

    for team_id in (team_ids["1"], team_ids["2"]):
        valid, code, reason = validate_lineup_state(current_lineups[team_id], team_id, allowed_players[team_id])
        if not valid:
            return {
                "game_id": game_id,
                "root_cause": FAILURE_MISSING_STARTER if code == FAILURE_WRONG_TEAM_PLAYER else code,
                "reason": reason,
                "period": 0,
                "clock": "",
                "action_number": 0,
                "event_id": None,
                "event_type": "starter_validation",
                "team_id": team_id,
                "player_id": None,
                "secondary_player_id": None,
                "lineup_before": [],
                "lineup_after": list(current_lineups[team_id]),
                "previous_event": None,
                "next_event": event_summary(None if not sorted_events else event_context_payload(sorted_events[0][0], sorted_events[0][1], connection, game_id, player_lookup, team_ids, game_data.get("tm") or {})),
            }

    for position, (index, event) in enumerate(sorted_events):
        context = event_context_payload(index, event, connection, game_id, player_lookup, team_ids, game_data.get("tm") or {})
        before_lineups = {
            team_ids["1"]: list(current_lineups[team_ids["1"]]),
            team_ids["2"]: list(current_lineups[team_ids["2"]]),
        }

        reason_code = None
        reason_message = None
        event_team_id = context["team_id"]
        operation = None
        resolved_out_player = None
        resolved_in_player = None
        prior_sub_count_for_team = 0

        if event_team_id in current_lineups:
            for earlier_index, earlier_event in sorted_events[:position]:
                if (
                    (normalize_text(earlier_event.get("actionType")) or "").lower() == "substitution"
                    and team_ids.get(normalize_text(earlier_event.get("tno")) or "") == event_team_id
                ):
                    prior_sub_count_for_team += 1

        if context["event_type"] == "substitution" and event_team_id in current_lineups:
            batch_key = (context["period"], clock_group_key(context["clock"]), event_team_id)
            if batch_key in processed_batches:
                continue
            processed_batches.add(batch_key)
            batch = batches[batch_key]
            first_substitution = batch[0]
            player_id = normalize_text(first_substitution.get("player_id")) or ""
            if (normalize_text(first_substitution.get("subType")) or "").lower() == "out":
                resolved_out_player = player_id or None
            else:
                resolved_in_player = player_id or None
            operation = f"APPLY_BATCH({[item['actionNumber'] for item in batch]})"
            try:
                current_lineups[event_team_id] = sorted(
                    apply_substitution_batch(
                        set(current_lineups[event_team_id]),
                        batch,
                        "home" if event_team_id == team_ids["1"] else "away",
                        context["period"],
                        context["clock"],
                        game_id=game_id,
                        allowed_players=allowed_players[event_team_id],
                    ),
                    key=natural_player_sort_key,
                )
            except LineupTransitionError as exc:
                reason_code = exc.code
                reason_message = exc.context["reason"]
                operation = f"REJECT_BATCH({[item['actionNumber'] for item in batch]})"

        after_lineups = {
            team_ids["1"]: list(current_lineups[team_ids["1"]]),
            team_ids["2"]: list(current_lineups[team_ids["2"]]),
        }

        if reason_code is None:
            for team_id in (team_ids["1"], team_ids["2"]):
                valid, code, reason = validate_lineup_state(after_lineups[team_id], team_id, allowed_players[team_id])
                if not valid:
                    reason_code = code
                    reason_message = reason
                    if (
                        position > 0
                        and logical_period_number(sorted_events[position - 1][1]) != context["period"]
                        and code in {FAILURE_INVALID_SIZE, FAILURE_DUPLICATE_PLAYER}
                    ):
                        reason_code = FAILURE_PERIOD_TRANSITION_ERROR
                        reason_message = f"period transition invalid state: {reason}"
                    event_team_id = team_id
                    break

        if reason_code is not None:
            previous_context = None
            if position > 0:
                previous_context = event_context_payload(
                    sorted_events[position - 1][0],
                    sorted_events[position - 1][1],
                    connection,
                    game_id,
                    player_lookup,
                    team_ids,
                )

            next_context = None
            if position + 1 < len(sorted_events):
                next_context = event_context_payload(
                    sorted_events[position + 1][0],
                    sorted_events[position + 1][1],
                    connection,
                    game_id,
                    player_lookup,
                    team_ids,
                )

            return {
                "game_id": game_id,
                "root_cause": reason_code,
                "reason": reason_message or "",
                "period": context["period"],
                "clock": context["clock"],
                "action_number": context["action_number"],
                "event_id": context["event_id"],
                "event_type": context["event_type"],
                "team_id": context["team_id"],
                "player_id": context["player_id"],
                "secondary_player_id": context["secondary_player_id"],
                "lineup_before": before_lineups.get(event_team_id, []),
                "lineup_after": after_lineups.get(event_team_id, []),
                "previous_event": event_summary(previous_context),
                "next_event": event_summary(next_context),
                "metadata_json": context["metadata_json"],
                "live_payload_json": context["live_payload_json"],
                "resolved_out_player": resolved_out_player,
                "resolved_in_player": resolved_in_player,
                "operation": operation,
                "classification": classify_debug_root_cause(
                    {
                        "root_cause": reason_code,
                        "reason": reason_message,
                        "event_type": context["event_type"],
                        "operation": operation,
                        "lineup_after": after_lineups.get(event_team_id, []),
                        "period": context["period"],
                        "previous_event": event_summary(previous_context),
                    },
                    prior_sub_count_for_team,
                ),
            }

    return None


def run_debug_mode(connection: sqlite3.Connection) -> None:
    player_lookup = build_player_lookup(connection)
    pending_games = load_game_rows(connection)

    games_processed = 0
    games_fixed = 0
    games_still_failing = 0
    failures: list[dict] = []

    for game_row in pending_games:
        games_processed += 1
        try:
            failure = debug_first_invalid_event(connection, game_row, player_lookup)
        except Exception as exc:
            failure = {
                "game_id": game_row["game_id"],
                "root_cause": FAILURE_UNKNOWN,
                "reason": str(exc),
                "period": 0,
                "clock": "",
                "action_number": 0,
                "event_id": None,
                "event_type": "debug_exception",
                "team_id": None,
                "player_id": None,
                "secondary_player_id": None,
                "lineup_before": [],
                "lineup_after": [],
                "previous_event": "none",
                "next_event": "none",
                "metadata_json": None,
                "live_payload_json": None,
                "resolved_out_player": None,
                "resolved_in_player": None,
                "operation": None,
                "classification": "I",
            }

        if failure is None:
            games_fixed += 1
            continue

        games_still_failing += 1
        failures.append(failure)

        print(f"game_id={failure['game_id']}")
        print(f"period={failure['period']}")
        print(f"clock={failure['clock']}")
        print(f"action_number={failure['action_number']}")
        print(f"event_id={failure['event_id']}")
        print(f"event_type={failure['event_type']}")
        print(f"team_id={failure['team_id']}")
        print(f"player_id={failure['player_id']}")
        print(f"secondary_player_id={failure['secondary_player_id']}")
        print(f"current_lineup_before_event={','.join(failure['lineup_before'])}")
        print(f"current_lineup_after_event={','.join(failure['lineup_after'])}")
        print(f"previous_event={failure['previous_event']}")
        print(f"next_event={failure['next_event']}")
        print(f"raw_metadata_json={failure['metadata_json']}")
        if failure["event_type"] == "substitution":
            print(f"livestats_substitution_payload={failure['live_payload_json']}")
            print(f"resolved_OUT_player={failure['resolved_out_player']}")
            print(f"resolved_IN_player={failure['resolved_in_player']}")
            print(f"reconstruction_operation_performed={failure['operation']}")
        print(f"reason_for_failure={failure['root_cause']}|{failure['reason']}")
        print(f"classified_root_cause={failure['classification']}")

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for failure in failures:
        grouped[(failure["classification"], failure["reason"])].append(failure)

    print(f"games_processed={games_processed}")
    print(f"games_fixed={games_fixed}")
    print(f"games_still_failing={games_still_failing}")

    for (root_cause, reason), group in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0][0], item[0][1])):
        example_game_ids = ",".join(item["game_id"] for item in group[:10])
        first = group[0]
        print(f"root_cause={root_cause}")
        print(f"count={len(group)}")
        print(f"example_game_ids={example_game_ids}")
        print(
            "first_invalid_event="
            f"game_id:{first['game_id']}|period:{first['period']}|clock:{first['clock']}|"
            f"action_number:{first['action_number']}|event_id:{first['event_id']}|event_type:{first['event_type']}|"
            f"team_id:{first['team_id']}|player_id:{first['player_id']}|secondary_player_id:{first['secondary_player_id']}|"
            f"reason:{first['reason']}"
        )


def build_team_roster(
    game_data: dict,
    team_number: str,
    connection: sqlite3.Connection,
    player_lookup: dict[str, str],
    game_id: str | None = None,
    team_id: str | None = None,
) -> list[str]:
    team = game_data.get("tm", {}).get(team_number)
    if not team:
        raise ValueError(f"Missing team data for team number {team_number}")

    if game_id is None or team_id is None:
        raise LineupTransitionError(
            FAILURE_UNRESOLVED_PLAYER_IDENTITY,
            f"{FAILURE_UNRESOLVED_PLAYER_IDENTITY}: game and team context are required to resolve starters",
        )

    starters: list[str] = []
    for player in team.get("pl", {}).values():
        if int(player.get("starter") or 0) != 1:
            continue
        provider_occurrence = {
            "tno": team_number,
            "player": player.get("name") or player.get("scoreboardName"),
            "playerId": player.get("playerId"),
            "pno": player.get("pno"),
            "shirtNumber": player.get("shirtNumber"),
        }
        player_id = resolve_substitution_occurrence(
            connection,
            game_id,
            team_id,
            provider_occurrence,
            {team_number: team},
        )
        starters.append(player_id)

    if len(starters) != 5:
        raise LineupTransitionError(
            FAILURE_MISSING_STARTER,
            f"{FAILURE_MISSING_STARTER}: expected 5 provider starters for team {team_number}, found {len(starters)}",
        )
    if len(set(starters)) != 5:
        raise LineupTransitionError(
            FAILURE_DUPLICATE_PLAYER,
            f"{FAILURE_DUPLICATE_PLAYER}: provider starter occurrences map to duplicate ARC player IDs for team {team_number}",
        )

    persisted = connection.execute(
        """
        SELECT player_id, provider_player_id, player_name, shirt_number
        FROM player_games
        WHERE game_id = ? AND team_id = ? AND starter = 1
        ORDER BY player_game_id
        """,
        (game_id, team_id),
    ).fetchall()
    persisted_ids = [str(row[0]) for row in persisted if row[0] is not None]
    if len(persisted) != 5 or len(persisted_ids) != 5 or set(persisted_ids) != set(starters):
        raise LineupTransitionError(
            FAILURE_PLAYER_IDENTITY_CONFLICT,
            f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: persisted starter mappings disagree with provider roster for game {game_id}, team {team_id}",
        )

    return sorted(starters, key=natural_player_sort_key)


def lineup_hash_for_players(player_ids: list[str]) -> str:
    raw_value = "|".join(sorted(player_ids, key=natural_player_sort_key))
    return hashlib.sha1(raw_value.encode("utf-8")).hexdigest()


def get_or_create_lineup(
    connection: sqlite3.Connection,
    lineup_cache: dict[tuple[str, str], int],
    team_id: str,
    lineup_players: list[str],
) -> tuple[int, bool]:
    if len(lineup_players) != 5 or len(set(lineup_players)) != 5:
        raise ValueError(
            f"Cannot persist lineup for team {team_id}: expected five distinct player IDs, got {format_lineup(lineup_players)}"
        )
    normalized_players = sorted(lineup_players, key=natural_player_sort_key)
    lineup_hash = lineup_hash_for_players(normalized_players)
    team_key = str(team_id)
    cache_key = (team_key, lineup_hash)

    if cache_key in lineup_cache:
        return lineup_cache[cache_key], False

    existing = connection.execute(
        "SELECT lineup_id FROM lineups WHERE team_id = ? AND lineup_hash = ?",
        (team_key, lineup_hash),
    ).fetchone()
    if existing:
        lineup_id = int(existing[0])
        lineup_cache[cache_key] = lineup_id
        return lineup_id, False

    try:
        cursor = connection.execute(
            """
            INSERT INTO lineups (
                team_id,
                player_1,
                player_2,
                player_3,
                player_4,
                player_5,
                lineup_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (team_key, *normalized_players, lineup_hash),
        )
        lineup_id = int(cursor.lastrowid)
        lineup_cache[cache_key] = lineup_id
        return lineup_id, True
    except sqlite3.IntegrityError:
        existing = connection.execute(
            "SELECT lineup_id FROM lineups WHERE team_id = ? AND lineup_hash = ?",
            (team_key, lineup_hash),
        ).fetchone()
        if existing:
            lineup_id = int(existing[0])
            lineup_cache[cache_key] = lineup_id
            return lineup_id, False
        raise


def apply_substitution_batch(
    current_lineup: set[str],
    substitutions: list[dict],
    team_label: str,
    period: int,
    clock: str,
    validate_size: bool = True,
    game_id: str | None = None,
    allowed_players: set[str] | None = None,
) -> set[str]:
    ordered_substitutions = list(substitutions)
    before = set(current_lineup)
    batch_actions = [int(event["actionNumber"]) for event in ordered_substitutions]
    batch_members = [
        {
            "action": int(event["actionNumber"]),
            "direction": (normalize_text(event.get("subType")) or "").lower(),
            "player_id": normalize_text(event.get("player_id")),
        }
        for event in ordered_substitutions
    ]

    def fail(code: str, reason: str) -> LineupTransitionError:
        context = {
            "game_id": game_id,
            "team": team_label,
            "period": period,
            "clock": clock,
            "action_numbers": batch_actions,
            "lineup_before": sorted(before, key=natural_player_sort_key),
            "batch": batch_members,
            "reason": reason,
        }
        return LineupTransitionError(
            code,
            f"{code}: game={game_id or '?'} team={team_label} period={period} clock={clock} "
            f"actions={batch_actions} lineup_before={format_lineup(before)} "
            f"batch={json.dumps(batch_members, ensure_ascii=True)} reason={reason}",
            context,
        )

    if not ordered_substitutions:
        raise fail(FAILURE_SUBSTITUTION_BATCH_ERROR, "empty substitution batch")
    if validate_size and len(before) != 5:
        raise fail(FAILURE_INVALID_SIZE, f"lineup before batch has {len(before)} players")

    after = set(before)
    for event in ordered_substitutions:
        sub_type = (normalize_text(event.get("subType")) or "").lower()
        player_id = normalize_text(event.get("player_id"))
        action_number = int(event["actionNumber"])
        if not player_id:
            raise fail(
                FAILURE_SUBSTITUTION_BATCH_ERROR,
                f"substitution player ID is unresolved at action {action_number}",
            )
        if sub_type == "out":
            if player_id not in after:
                raise fail(
                    FAILURE_OUT_PLAYER_NOT_PRESENT,
                    f"outgoing players are not active at action {action_number}: {[player_id]}",
                )
            after.remove(player_id)
        elif sub_type == "in":
            if player_id in after:
                raise fail(
                    FAILURE_IN_PLAYER_ALREADY_PRESENT,
                    f"incoming players are already active at action {action_number}: {[player_id]}",
                )
            after.add(player_id)
        else:
            raise fail(
                FAILURE_SUBSTITUTION_BATCH_ERROR,
                f"invalid substitution direction {sub_type!r} at action {action_number}",
            )

        if allowed_players is not None:
            wrong_team = after.difference(allowed_players)
            if wrong_team:
                raise fail(
                    FAILURE_WRONG_TEAM_PLAYER,
                    f"players not on team {team_label} after action {action_number}: "
                    f"{sorted(wrong_team, key=natural_player_sort_key)}",
                )

    if validate_size and len(after) != 5:
        raise fail(FAILURE_INVALID_SIZE, f"lineup after batch has {len(after)} players")
    return after


def find_next_incoming_before_resume(
    events_in_period: list[tuple[int, dict]],
    team_number: str,
    after_source_index: int,
) -> int | None:
    after_index_found = False
    for index, event in events_in_period:
        if not after_index_found:
            if index == after_source_index:
                after_index_found = True
            continue

        action_type = (normalize_text(event.get("actionType")) or "").lower()
        if action_type == "substitution":
            event_team_number = normalize_text(event.get("tno")) or ""
            sub_type = (normalize_text(event.get("subType")) or "").lower()
            if event_team_number == team_number and sub_type == "in":
                return int(event.get("actionNumber") or index)
            continue

        if action_type in DEADBALL_EVENT_TYPES:
            continue

        return None

    return None


def prepare_team_substitutions(
    period_events: list[tuple[int, dict]],
    team_number: str,
    connection: sqlite3.Connection,
    player_lookup: dict[str, str],
    game_id: str | None = None,
    team_id: str | None = None,
    team_rosters: dict | None = None,
) -> list[dict]:
    substitutions: list[dict] = []

    for index, event in period_events:
        action_type = normalize_text(event.get("actionType"))
        event_is_substitution = (action_type or "").lower() == "substitution"
        if not event_is_substitution:
            continue

        source_team_number = normalize_text(event.get("tno"))
        if source_team_number not in {"1", "2"}:
            raise ValueError(f"Missing or invalid substitution team for action {event.get('actionNumber')}")
        if source_team_number != team_number:
            continue

        if event.get("success") is not None:
            try:
                success = int(event["success"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid substitution success for action {event.get('actionNumber')}") from exc
            if success != 1:
                raise ValueError(f"Unsuccessful substitution event at action {event.get('actionNumber')}")

        current_clock = normalize_text(event.get("clock"))
        clock_parts = current_clock.split(":") if current_clock else []
        if len(clock_parts) not in (2, 3) or not all(part.isdigit() for part in clock_parts):
            raise ValueError(
                f"Missing or invalid substitution clock for action {event.get('actionNumber')}"
            )

        sub_type = normalize_text(event.get("subType"))
        if sub_type not in {"in", "out"}:
            raise ValueError(f"Missing substitution direction for action {event.get('actionNumber')}")

        player_id = None
        if game_id is not None:
            persisted = connection.execute(
                """
                SELECT player_id, team_id, metadata_json
                FROM events
                WHERE game_id = ? AND action_number = ?
                LIMIT 1
                """,
                (game_id, int(event.get("actionNumber") or index)),
            ).fetchone()
            player_id = str(persisted[0]) if persisted and persisted[0] is not None else None
            persisted_team_id = str(persisted[1]) if persisted and persisted[1] is not None else None
            persisted_metadata = normalize_text(persisted[2]) if persisted else None
            if persisted_team_id is not None and persisted_team_id != str(team_id):
                raise LineupTransitionError(
                    FAILURE_PLAYER_IDENTITY_CONFLICT,
                    f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: persisted event team {persisted_team_id} disagrees with provider team {team_id} at action {event.get('actionNumber')}",
                )

        if not team_id or team_rosters is None or game_id is None:
            raise LineupTransitionError(
                FAILURE_UNRESOLVED_PLAYER_IDENTITY,
                f"{FAILURE_UNRESOLVED_PLAYER_IDENTITY}: no game/team roster context for action {event.get('actionNumber')}",
            )
        identity_event, _ = event_identity_with_persisted_evidence(event, persisted_metadata if game_id is not None else None)
        resolved_player_id = resolve_substitution_occurrence(
            connection,
            str(game_id),
            str(team_id),
            identity_event,
            team_rosters,
        )
        if player_id is not None and player_id != resolved_player_id:
            raise LineupTransitionError(
                FAILURE_PLAYER_IDENTITY_CONFLICT,
                f"{FAILURE_PLAYER_IDENTITY_CONFLICT}: persisted player_id {player_id} disagrees with provider occurrence {resolved_player_id} at action {event.get('actionNumber')}",
            )
        player_id = resolved_player_id

        substitutions.append(
            {
                "index": index,
                "actionNumber": int(event.get("actionNumber") or index),
                "clock": current_clock,
                "period": logical_period_number(event),
                "subType": sub_type,
                "player_id": player_id,
            }
        )

    return substitutions


def group_substitutions_by_batch(substitutions: list[dict]) -> list[list[dict]]:
    """Group same-team substitutions sharing one logical-period clock stoppage."""
    if not substitutions:
        return []

    grouped_by_key: dict[tuple[int, tuple[int, int, int] | tuple[str]], list[dict]] = {}
    key_order: list[tuple[int, tuple[int, int, int] | tuple[str]]] = []

    for substitution in substitutions:
        substitution_key = (
            int(substitution["period"]),
            clock_group_key(substitution.get("clock")),
        )
        if substitution_key not in grouped_by_key:
            grouped_by_key[substitution_key] = []
            key_order.append(substitution_key)
        grouped_by_key[substitution_key].append(substitution)

    return [grouped_by_key[key] for key in key_order]


def diagnostic_substitution_batches(
    sorted_events: list[tuple[int, dict]],
    connection: sqlite3.Connection,
    game_id: str,
    player_lookup: dict[str, str],
    team_ids: dict[str, str],
    team_rosters: dict | None = None,
) -> dict[tuple[int, tuple[int, int, int] | tuple[str], str], list[dict]]:
    substitutions: dict[tuple[int, tuple[int, int, int] | tuple[str], str], list[dict]] = defaultdict(list)
    for index, event in sorted_events:
        if (normalize_text(event.get("actionType")) or "").lower() != "substitution":
            continue
        team_number = normalize_text(event.get("tno")) or ""
        team_id = team_ids.get(team_number)
        if not team_id:
            continue

        context = event_context_payload(index, event, connection, game_id, player_lookup, team_ids, team_rosters)
        period = logical_period_number(event)
        clock = normalize_text(event.get("clock")) or "00:00:00"
        key = (period, clock_group_key(clock), team_id)
        substitutions[key].append(
            {
                "index": index,
                "actionNumber": int(event.get("actionNumber") or index),
                "clock": clock,
                "period": period,
                "subType": (normalize_text(event.get("subType")) or "").lower(),
                "player_id": normalize_text(context["player_id"]),
                "live_payload_json": context["live_payload_json"],
            }
        )

    return {
        key: group
        for key, items in substitutions.items()
        for group in group_substitutions_by_batch(items)
    }


def insert_segment(
    connection: sqlite3.Connection,
    lineup_segment_cache: dict[str, int],
    lineup_id: int,
    game_id: str,
    team_id: str,
    period: int,
    start_clock: str,
    end_clock: str,
    start_action_number: int | None,
    end_action_number: int | None,
) -> tuple[int, bool]:
    cache_key = f"{game_id}|{team_id}|{period}|{start_clock}|{end_clock}|{lineup_id}"
    if cache_key in lineup_segment_cache:
        return lineup_segment_cache[cache_key], False

    duration_seconds = clock_duration_seconds(start_clock, end_clock)
    columns = [
        "game_id",
        "lineup_id",
        "team_id",
        "period",
        "start_action_number",
        "end_action_number",
        "start_clock",
        "end_clock",
    ]
    values = [
        game_id,
        lineup_id,
        team_id,
        period,
        start_action_number,
        end_action_number,
        start_clock,
        end_clock,
    ]

    if table_has_column(connection, "lineup_segments", "duration_seconds"):
        columns.append("duration_seconds")
        values.append(duration_seconds)

    cursor = connection.execute(
        f"""
        INSERT INTO lineup_segments ({', '.join(columns)})
        VALUES ({', '.join(['?'] * len(values))})
        """,
        values,
    )
    segment_id = int(cursor.lastrowid)
    lineup_segment_cache[cache_key] = segment_id
    return segment_id, True


def process_game(
    connection: sqlite3.Connection,
    game_row: sqlite3.Row,
    player_lookup: dict[str, str],
    lineup_cache: dict[tuple[str, str], int],
    lineup_segment_cache: dict[str, int],
) -> dict[str, int | str]:
    """Reconstruct one game atomically, nesting safely inside caller transactions."""
    lineup_cache_before = dict(lineup_cache)
    segment_cache_before = dict(lineup_segment_cache)
    savepoint = "arc_lineup_reconstruction"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        result = _process_game(
            connection,
            game_row,
            player_lookup,
            lineup_cache,
            lineup_segment_cache,
        )
        if "normalization_id" in game_row.keys() and game_row["normalization_id"] is not None:
            _persist_reconstruction_run(connection, game_row)
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        lineup_cache.clear()
        lineup_cache.update(lineup_cache_before)
        lineup_segment_cache.clear()
        lineup_segment_cache.update(segment_cache_before)
        raise


def _identity_interpretation_sha256(connection: sqlite3.Connection, decision_set_id: int) -> str:
    """Hash only the identity assignments and statuses that affect reconstruction.

    Decision-set hashes also preserve resolver evidence and confidence for audit
    history. Those metadata can change without changing the identity mapping.
    """
    rows = connection.execute(
        """SELECT occurrence.identity_occurrence_key, decision.arc_player_id, decision.status
           FROM identity_decisions decision
           JOIN source_player_occurrences occurrence
             ON occurrence.player_occurrence_id = decision.player_occurrence_id
           WHERE decision.decision_set_id = ?
           ORDER BY occurrence.identity_occurrence_key""",
        (decision_set_id,),
    ).fetchall()
    semantic_decisions = [
        (str(row[0]), int(row[1]) if row[1] is not None else None, str(row[2]))
        for row in rows
    ]
    payload = json.dumps(semantic_decisions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reconstruction_input_sha256(connection: sqlite3.Connection, game_row: sqlite3.Row) -> str:
    input_components = [
        str(game_row["captured_body_sha256"]),
        str(game_row["normalized_sha256"]),
        str(game_row["normalizer_version"]),
        _identity_interpretation_sha256(connection, int(game_row["decision_set_id"])),
        RECONSTRUCTION_ALGORITHM_VERSION,
    ]
    return hashlib.sha256("|".join(input_components).encode("utf-8")).hexdigest()


def _persist_reconstruction_run(connection: sqlite3.Connection, game_row: sqlite3.Row) -> int:
    game_id = str(game_row["game_id"])
    normalization_id = int(game_row["normalization_id"])
    snapshot_id = int(game_row["snapshot_id"])
    decision_set_id = int(game_row["decision_set_id"])
    input_hash = _reconstruction_input_sha256(connection, game_row)
    output_rows = connection.execute(
        """SELECT s.team_id, s.period, s.start_action_number, s.end_action_number,
                  s.start_clock, s.end_clock, s.duration_seconds, s.lineup_id,
                  l.player_1, l.player_2, l.player_3, l.player_4, l.player_5
           FROM lineup_segments s JOIN lineups l ON l.lineup_id=s.lineup_id WHERE s.game_id = ?
           ORDER BY s.team_id, s.period, s.start_action_number, s.start_clock, s.end_action_number""",
        (game_id,),
    ).fetchall()
    semantic_output = [tuple(row[:7]) + tuple(row[8:]) for row in output_rows]
    output_hash = hashlib.sha256(json.dumps(semantic_output, separators=(",", ":")).encode("utf-8")).hexdigest()
    cursor = connection.execute(
        """INSERT INTO reconstruction_runs
           (game_id, snapshot_id, normalization_id, decision_set_id, algorithm_version,
            input_sha256, output_sha256, status, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', CURRENT_TIMESTAMP)""",
        (game_id, snapshot_id, normalization_id, decision_set_id, RECONSTRUCTION_ALGORITHM_VERSION, input_hash, output_hash),
    )
    run_id = int(cursor.lastrowid)
    for row in output_rows:
        connection.execute(
            """INSERT INTO reconstruction_run_segments
               (reconstruction_run_id, game_id, lineup_id, team_id, period,
                start_action_number, end_action_number, start_clock, end_clock, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, game_id, row[7], row[0], row[1], row[2], row[3], row[4], row[5], row[6]),
        )
    connection.execute(
        """INSERT INTO active_reconstructions(game_id, reconstruction_run_id)
           VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET
             reconstruction_run_id=excluded.reconstruction_run_id, activated_at=CURRENT_TIMESTAMP""",
        (game_id, run_id),
    )
    return run_id


def record_failed_reconstruction(connection: sqlite3.Connection, game_row: sqlite3.Row, error: Exception) -> None:
    if "normalization_id" not in game_row.keys() or game_row["normalization_id"] is None:
        return
    input_hash = _reconstruction_input_sha256(connection, game_row)
    connection.execute(
        """INSERT INTO reconstruction_runs
           (game_id,snapshot_id,normalization_id,decision_set_id,algorithm_version,
            input_sha256,status,started_at,completed_at,error_message)
           VALUES(?,?,?,?,?,?,'failed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?)""",
        (str(game_row["game_id"]), int(game_row["snapshot_id"]), int(game_row["normalization_id"]),
         int(game_row["decision_set_id"]), RECONSTRUCTION_ALGORITHM_VERSION, input_hash, str(error)),
    )


def _process_game(
    connection: sqlite3.Connection,
    game_row: sqlite3.Row,
    player_lookup: dict[str, str],
    lineup_cache: dict[tuple[str, str], int],
    lineup_segment_cache: dict[str, int],
) -> dict[str, int | str]:
    game_data = load_source_snapshot_data(connection, game_row)
    sorted_events = extract_sorted_events(game_data)
    period_events = group_period_events(sorted_events)

    home_team_id = normalize_text(game_row["home_team_id"])
    away_team_id = normalize_text(game_row["away_team_id"])
    if not home_team_id or not away_team_id:
        raise ValueError(f"Missing team ids for game {game_row['game_id']}")

    team_ids = {"1": home_team_id, "2": away_team_id}
    team_labels = {"1": "home", "2": "away"}
    allowed_players_by_team = {
        team_id: {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT player_id FROM player_games WHERE game_id = ? AND team_id = ? AND player_id IS NOT NULL",
                (game_row["game_id"], team_id),
            )
        }
        for team_id in (home_team_id, away_team_id)
    }

    current_lineups: dict[str, set[str]] = {
        team_number: set(
            build_team_roster(
                game_data,
                team_number,
                connection,
                player_lookup,
                game_row["game_id"],
                team_ids[team_number],
            )
        )
        for team_number in ("1", "2")
    }

    for team_number in ("1", "2"):
        valid, code, reason = validate_lineup_state(
            list(current_lineups[team_number]),
            team_ids[team_number],
            allowed_players_by_team[team_ids[team_number]],
        )
        if not valid:
            raise LineupTransitionError(
                code,
                f"{code}: game={game_row['game_id']} team={team_labels[team_number]} "
                f"period=0 clock= lineup_before={format_lineup(current_lineups[team_number])} "
                f"reason=invalid starter lineup: {reason}",
            )

    def apply_batch(
        lineup: set[str],
        substitutions: list[dict],
        team_id: str,
        team_label: str,
        period: int,
        clock: str,
        validate_size: bool = True,
    ) -> set[str]:
        return apply_substitution_batch(
            lineup,
            substitutions,
            team_label,
            period,
            clock,
            validate_size=validate_size,
            game_id=str(game_row["game_id"]),
            allowed_players=allowed_players_by_team[team_id],
        )

    lineups_created = 0
    segments_created = 0

    for period in sorted(period_events):
        events_in_period = period_events[period]
        period_start_clock = normalize_text(events_in_period[0][1].get("clock")) or "10:00:00"
        period_end_clock = normalize_text(events_in_period[-1][1].get("clock")) or "00:00:00"
        period_start_action_number = int(events_in_period[0][1].get("actionNumber") or events_in_period[0][0])
        period_end_action_number = int(events_in_period[-1][1].get("actionNumber") or events_in_period[-1][0])

        for team_number in ("1", "2"):
            team_id = team_ids[team_number]
            team_label = team_labels[team_number]
            team_substitutions = prepare_team_substitutions(
                events_in_period,
                team_number,
                connection,
                player_lookup,
                game_row["game_id"],
                team_ids[team_number],
                game_data.get("tm") or {},
            )
            grouped_substitutions = group_substitutions_by_batch(team_substitutions)

            period_start_key = clock_group_key(period_start_clock)
            period_end_key = clock_group_key(period_end_clock)
            start_groups = [
                group
                for group in grouped_substitutions
                if clock_group_key(group[0]["clock"]) == period_start_key
            ]
            middle_groups = [
                group
                for group in grouped_substitutions
                if clock_group_key(group[0]["clock"]) not in {period_start_key, period_end_key}
            ]
            end_groups = [
                group
                for group in grouped_substitutions
                if clock_group_key(group[0]["clock"]) == period_end_key
            ]

            lineup = set(current_lineups[team_number])
            consumed_middle_groups = 0
            consumed_end_groups = 0
            if start_groups:
                start_substitutions = [substitution for group in start_groups for substitution in group]
                try:
                    lineup = apply_batch(
                        lineup,
                        start_substitutions,
                        team_id,
                        team_label,
                        period,
                        period_start_clock,
                    )
                except LineupTransitionError as exc:
                    if exc.code != FAILURE_INVALID_SIZE:
                        raise

                    lineup_before_start = sorted(lineup, key=natural_player_sort_key)
                    lineup_after_remove_set = apply_batch(
                        set(lineup),
                        start_substitutions,
                        team_id,
                        team_label,
                        period,
                        period_start_clock,
                        validate_size=False,
                    )
                    if len(lineup_after_remove_set) != 4:
                        raise

                    next_incoming_action = find_next_incoming_before_resume(
                        events_in_period,
                        team_number,
                        start_substitutions[-1]["index"],
                    )
                    if next_incoming_action is None:
                        raise

                    involved_action_numbers = [substitution["actionNumber"] for substitution in start_substitutions]
                    lineup_after_add_set = set(lineup_after_remove_set)
                    resolved = False
                    tail_groups = list(middle_groups) + list(end_groups)
                    consume_index = 0

                    while consume_index < len(tail_groups):
                        deferred_group = tail_groups[consume_index]
                        deferred_action_numbers = [substitution["actionNumber"] for substitution in deferred_group]
                        lineup_after_add_set = apply_batch(
                            lineup_after_add_set,
                            deferred_group,
                            team_id,
                            team_label,
                            period,
                            deferred_group[0]["clock"],
                            validate_size=False,
                        )
                        involved_action_numbers.extend(deferred_action_numbers)

                        if next_incoming_action in deferred_action_numbers and len(lineup_after_add_set) == 5:
                            resolved = True
                            break

                        consume_index += 1

                    if not resolved:
                        raise

                    print("DEFERRED_VALIDATION")
                    print(f"game_id={game_row['game_id']}")
                    print(f"period={period}")
                    print(f"clock={period_start_clock}")
                    print(f"action_numbers_involved={','.join(str(value) for value in sorted(set(involved_action_numbers)))}")
                    print(f"lineup_before={','.join(lineup_before_start)}")
                    print(f"lineup_after_remove={','.join(sorted(lineup_after_remove_set, key=natural_player_sort_key))}")
                    print(f"lineup_after_add={','.join(sorted(lineup_after_add_set, key=natural_player_sort_key))}")
                    print("defer_reason=incoming substitution exists for same team before play resumes")

                    lineup = set(lineup_after_add_set)
                    groups_consumed = consume_index + 1
                    consumed_middle_groups = min(groups_consumed, len(middle_groups))
                    consumed_end_groups = max(0, groups_consumed - len(middle_groups))

            current_segment_start_clock = period_start_clock
            current_segment_start_action_number = (
                start_groups[-1][-1]["actionNumber"]
                if start_groups
                else period_start_action_number
            )

            middle_index = consumed_middle_groups
            while middle_index < len(middle_groups):
                group = middle_groups[middle_index]
                group_clock = group[0]["clock"]
                group_action_numbers = [substitution["actionNumber"] for substitution in group]

                lineup_id, created = get_or_create_lineup(
                    connection,
                    lineup_cache,
                    team_id,
                    sorted(lineup, key=natural_player_sort_key),
                )
                if created:
                    lineups_created += 1

                _, created_segment = insert_segment(
                    connection,
                    lineup_segment_cache,
                    lineup_id,
                    game_row["game_id"],
                    team_id,
                    period,
                    current_segment_start_clock,
                    group_clock,
                    current_segment_start_action_number,
                    group[0]["actionNumber"],
                )
                if created_segment:
                    segments_created += 1

                try:
                    lineup = apply_batch(lineup, group, team_id, team_label, period, group_clock)
                    current_segment_start_clock = group_clock
                    current_segment_start_action_number = group[-1]["actionNumber"]
                    middle_index += 1
                    continue
                except LineupTransitionError as exc:
                    if exc.code != FAILURE_INVALID_SIZE:
                        raise

                    lineup_before = sorted(lineup, key=natural_player_sort_key)
                    lineup_after_remove_set = apply_batch(
                        set(lineup),
                        group,
                        team_id,
                        team_label,
                        period,
                        group_clock,
                        validate_size=False,
                    )

                    if len(lineup_after_remove_set) != 4:
                        raise

                    next_incoming_action = find_next_incoming_before_resume(
                        events_in_period,
                        team_number,
                        group[-1]["index"],
                    )
                    if next_incoming_action is None:
                        raise

                    involved_action_numbers = list(group_action_numbers)
                    lineup_after_add_set = set(lineup_after_remove_set)
                    resolved = False
                    remaining_middle_groups = middle_groups[middle_index + 1 :]
                    tail_groups = list(remaining_middle_groups) + list(end_groups)
                    consume_index = 0

                    while consume_index < len(tail_groups):
                        deferred_group = tail_groups[consume_index]
                        deferred_action_numbers = [substitution["actionNumber"] for substitution in deferred_group]
                        lineup_after_add_set = apply_batch(
                            lineup_after_add_set,
                            deferred_group,
                            team_id,
                            team_label,
                            period,
                            deferred_group[0]["clock"],
                            validate_size=False,
                        )
                        involved_action_numbers.extend(deferred_action_numbers)

                        if next_incoming_action in deferred_action_numbers and len(lineup_after_add_set) == 5:
                            resolved = True
                            break

                        consume_index += 1

                    if not resolved:
                        raise

                    print("DEFERRED_VALIDATION")
                    print(f"game_id={game_row['game_id']}")
                    print(f"period={period}")
                    print(f"clock={group_clock}")
                    print(f"action_numbers_involved={','.join(str(value) for value in sorted(set(involved_action_numbers)))}")
                    print(f"lineup_before={','.join(lineup_before)}")
                    print(f"lineup_after_remove={','.join(sorted(lineup_after_remove_set, key=natural_player_sort_key))}")
                    print(f"lineup_after_add={','.join(sorted(lineup_after_add_set, key=natural_player_sort_key))}")
                    print("defer_reason=incoming substitution exists for same team before play resumes")

                    lineup = set(lineup_after_add_set)
                    resolved_group = tail_groups[consume_index]
                    current_segment_start_clock = resolved_group[0]["clock"]
                    current_segment_start_action_number = resolved_group[-1]["actionNumber"]
                    groups_consumed = consume_index + 1
                    consumed_from_middle_tail = min(groups_consumed, len(remaining_middle_groups))
                    consumed_from_end = max(0, groups_consumed - len(remaining_middle_groups))
                    consumed_end_groups = max(consumed_end_groups, consumed_from_end)
                    middle_index = middle_index + 1 + consumed_from_middle_tail

            lineup_id, created = get_or_create_lineup(
                connection,
                lineup_cache,
                team_id,
                sorted(lineup, key=natural_player_sort_key),
            )
            if created:
                lineups_created += 1

            _, created_segment = insert_segment(
                connection,
                lineup_segment_cache,
                lineup_id,
                game_row["game_id"],
                team_id,
                period,
                current_segment_start_clock,
                period_end_clock,
                current_segment_start_action_number,
                period_end_action_number,
            )
            if created_segment:
                segments_created += 1

            remaining_end_groups = end_groups[consumed_end_groups:]
            if remaining_end_groups:
                lineup = apply_batch(
                    lineup,
                    [substitution for group in remaining_end_groups for substitution in group],
                    team_id,
                    team_label,
                    period,
                    period_end_clock,
                )

            current_lineups[team_number] = lineup

    return {
        "game_id": game_row["game_id"],
        "segments_created": segments_created,
        "lineups_created": lineups_created,
    }


def validate_one_game(
    connection: sqlite3.Connection,
    game_row: sqlite3.Row,
    player_lookup: dict[str, str],
    lineup_cache: dict[tuple[str, str], int],
    lineup_segment_cache: dict[str, int],
) -> bool:
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = process_game(
            connection,
            game_row,
            player_lookup,
            lineup_cache,
            lineup_segment_cache,
        )
        connection.rollback()
        print(f"game_id={result['game_id']}")
        print(f"segments_created={result['segments_created']}")
        print(f"unique_lineups_created={result['lineups_created']}")
        print("validation=passed")
        return True
    except Exception:
        connection.rollback()
        print(f"game_id={game_row['game_id']}")
        print("segments_created=0")
        print("unique_lineups_created=0")
        print("validation=failed")
        return False


def run_single_game_mode(connection: sqlite3.Connection, game_id: str, force: bool = False, source_snapshot_id: int | None = None) -> None:
    """Reconstruct lineups for exactly one game, skipping games already reconstructed.

    A game with any existing lineup_segments row is treated as already
    processed, matching the "no existing lineup_segments" gate used by
    load_game_rows()/process_all_games() elsewhere in this module.
    """
    game_row = load_game_row_by_id(connection, game_id, source_snapshot_id)
    if game_row is None:
        print(f"game_id={game_id}")
        print("status=game_not_found")
        print("lineups_created=0")
        print("lineup_segments_created=0")
        raise SystemExit(1)

    already_reconstructed = connection.execute(
        "SELECT 1 FROM lineup_segments WHERE game_id = ? LIMIT 1",
        (game_id,),
    ).fetchone()
    if already_reconstructed and not force:
        print(f"game_id={game_id}")
        print("status=already_reconstructed")
        print("lineups_created=0")
        print("lineup_segments_created=0")
        return

    if already_reconstructed and force:
        active_run = connection.execute(
            "SELECT reconstruction_run_id FROM active_reconstructions WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        if active_run is None:
            print(f"game_id={game_id}")
            print("status=legacy_output_has_no_run_history")
            raise SystemExit(1)
        history_count = connection.execute(
            "SELECT COUNT(*) FROM reconstruction_run_segments WHERE reconstruction_run_id = ?",
            (active_run[0],),
        ).fetchone()[0]
        current_count = connection.execute(
            "SELECT COUNT(*) FROM lineup_segments WHERE game_id = ?",
            (game_id,),
        ).fetchone()[0]
        if history_count != current_count:
            print(f"game_id={game_id}")
            print("status=active_output_history_mismatch")
            raise SystemExit(1)

    player_lookup = build_player_lookup(connection)
    lineup_cache: dict[tuple[str, str], int] = {}
    for row in connection.execute("SELECT lineup_id, team_id, lineup_hash FROM lineups"):
        lineup_cache[(str(row[1]), row[2])] = int(row[0])
    lineup_segment_cache: dict[str, int] = {}

    try:
        connection.execute("BEGIN IMMEDIATE")
        if source_snapshot_id is not None:
            if not game_row["snapshot_id"] or not game_row["normalization_id"] or not game_row["decision_set_id"]:
                raise ValueError(f"Candidate source snapshot {source_snapshot_id} has no complete identity decision set")
            connection.execute("""INSERT INTO active_game_source_snapshots(game_id,snapshot_id,normalization_id)
                VALUES(?,?,?) ON CONFLICT(game_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,
                normalization_id=excluded.normalization_id""", (game_id, game_row["snapshot_id"], game_row["normalization_id"]))
            connection.execute("""INSERT INTO active_identity_decision_sets(game_id,normalization_id,decision_set_id)
                VALUES(?,?,?) ON CONFLICT(game_id) DO UPDATE SET normalization_id=excluded.normalization_id,
                decision_set_id=excluded.decision_set_id""", (game_id, game_row["normalization_id"], game_row["decision_set_id"]))
        if already_reconstructed and force:
            connection.execute("DELETE FROM lineup_segments WHERE game_id = ?", (game_id,))
        result = process_game(connection, game_row, player_lookup, lineup_cache, lineup_segment_cache)
        connection.commit()
    except Exception as exc:
        connection.rollback()
        try:
            record_failed_reconstruction(connection, game_row, exc)
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
        print(f"game_id={game_id}")
        print("status=failed")
        print("lineups_created=0")
        print("lineup_segments_created=0")
        print(f"error={exc}")
        raise SystemExit(1)

    print(f"game_id={game_id}")
    print("status=succeeded")
    print(f"lineups_created={result['lineups_created']}")
    print(f"lineup_segments_created={result['segments_created']}")


def process_all_games(connection: sqlite3.Connection) -> dict[str, int]:
    player_lookup = build_player_lookup(connection)
    lineup_cache: dict[tuple[str, str], int] = {}
    for row in connection.execute("SELECT lineup_id, team_id, lineup_hash FROM lineups"):
        lineup_cache[(str(row[1]), row[2])] = int(row[0])

    lineup_segment_cache: dict[str, int] = {}
    pending_games = load_game_rows(connection)

    games_processed = 0
    games_succeeded = 0
    games_failed = 0
    lineups_created = 0
    lineup_segments_created = 0
    runtime_errors = 0

    for game_row in pending_games:
        games_processed += 1
        lineup_cache_before = dict(lineup_cache)
        lineup_segment_cache_before = dict(lineup_segment_cache)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = process_game(connection, game_row, player_lookup, lineup_cache, lineup_segment_cache)
            connection.commit()
            games_succeeded += 1
            lineups_created += int(result["lineups_created"])
            lineup_segments_created += int(result["segments_created"])
        except Exception as failure:
            connection.rollback()
            try:
                record_failed_reconstruction(connection, game_row, failure)
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
            lineup_cache.clear()
            lineup_cache.update(lineup_cache_before)
            lineup_segment_cache.clear()
            lineup_segment_cache.update(lineup_segment_cache_before)
            games_failed += 1
            runtime_errors += 1

    return {
        "games_processed": games_processed,
        "games_succeeded": games_succeeded,
        "games_failed": games_failed,
        "lineups_created": lineups_created,
        "lineup_segments_created": lineup_segments_created,
        "runtime_errors": runtime_errors,
    }


_IDENTITY_CHANGE_TABLES = {
    "players": ("player_id",),
    "player_aliases": ("alias_id",),
    "player_external_ids": ("player_id", "provider", "external_player_id"),
    "team_external_ids": ("team_id", "provider"),
    "season_external_ids": ("season_id", "provider"),
    "league_external_ids": ("league_id", "provider"),
}


def identity_state_sha256(connection: sqlite3.Connection) -> str:
    state = {}
    for table in _IDENTITY_CHANGE_TABLES:
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
        state[table] = sorted(
            rows,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
    raw = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def apply_candidate_identity_changes(connection: sqlite3.Connection, changes: dict) -> None:
    for table, primary_key in _IDENTITY_CHANGE_TABLES.items():
        rows = changes.get(table, [])
        if not isinstance(rows, list):
            raise ValueError(f"Candidate identity changes for {table} are invalid")
        for row in rows:
            if not isinstance(row, dict) or not set(primary_key).issubset(row):
                raise ValueError(f"Candidate identity change for {table} lacks its primary key")
            columns = list(row)
            quoted = ", ".join(f'"{name}"' for name in columns)
            placeholders = ", ".join("?" for _ in columns)
            conflict = ", ".join(f'"{name}"' for name in primary_key)
            updates = [name for name in columns if name not in primary_key]
            action = (
                "DO UPDATE SET " + ", ".join(f'"{name}"=excluded."{name}"' for name in updates)
                if updates else "DO NOTHING"
            )
            connection.execute(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) ON CONFLICT ({conflict}) {action}',
                tuple(row[name] for name in columns),
            )


def insert_projection_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: list[dict],
    game_id: str,
    decision_ids_by_occurrence: dict[int, int],
) -> None:
    available = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    for source_row in rows:
        row = {key: value for key, value in source_row.items() if key in available}
        if table == "player_games" and row.get("source_player_occurrence_id") is not None:
            row["identity_decision_id"] = decision_ids_by_occurrence.get(int(row["source_player_occurrence_id"]))
        elif table == "events":
            primary = row.get("primary_source_player_occurrence_id")
            secondary = row.get("secondary_source_player_occurrence_id")
            row["primary_identity_decision_id"] = decision_ids_by_occurrence.get(int(primary)) if primary is not None else None
            row["secondary_identity_decision_id"] = decision_ids_by_occurrence.get(int(secondary)) if secondary is not None else None
        if row.get("game_id") != game_id:
            raise ValueError(f"Candidate {table} row belongs to a different game")
        columns = list(row)
        names = ", ".join(f'"{name}"' for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
            tuple(row[name] for name in columns),
        )


def insert_candidate_event_relationships(
    connection: sqlite3.Connection,
    game_id: str,
    normalization_id: int,
) -> int:
    source_rows = connection.execute(
        """SELECT event_occurrence_id, provider_action_number, previous_action_number,
                  provider_shot_previous_action
           FROM source_event_occurrences
           WHERE normalization_id=? AND (
             (previous_action_number IS NOT NULL AND TRIM(previous_action_number)<>'') OR
             (provider_shot_previous_action IS NOT NULL AND TRIM(provider_shot_previous_action)<>'')
           )
           ORDER BY source_ordinal""",
        (normalization_id,),
    ).fetchall()
    insert = connection.execute
    created = 0
    for source in source_rows:
        from_occurrence_id = int(source["event_occurrence_id"])
        from_event = connection.execute(
            "SELECT event_id FROM events WHERE game_id=? AND source_event_occurrence_id=?",
            (game_id, from_occurrence_id),
        ).fetchone()
        if from_event is None:
            raise ValueError(f"Event relationship source occurrence {from_occurrence_id} has no promoted event")

        references = []
        if source["previous_action_number"] is not None and str(source["previous_action_number"]).strip():
            references.append(("previousAction", str(source["previous_action_number"])))
        if source["provider_shot_previous_action"] is not None and str(source["provider_shot_previous_action"]).strip():
            references.append(("shot.previousAction", str(source["provider_shot_previous_action"])))
        for source_field, source_value in references:
            target_action_number = None
            try:
                target_action_number = int(source_value)
            except (TypeError, ValueError):
                pass
            target_occurrence = None
            target_event = None
            if target_action_number is not None:
                target_occurrence = connection.execute(
                    """SELECT event_occurrence_id FROM source_event_occurrences
                       WHERE normalization_id=? AND provider_action_number=? LIMIT 1""",
                    (normalization_id, target_action_number),
                ).fetchone()
                if target_occurrence is not None:
                    target_event = connection.execute(
                        "SELECT event_id FROM events WHERE game_id=? AND source_event_occurrence_id=?",
                        (game_id, int(target_occurrence[0])),
                    ).fetchone()

            insert(
                """INSERT INTO event_relationships
                   (game_id,from_event_id,to_event_id,from_source_event_occurrence_id,
                    to_source_event_occurrence_id,relationship_type,source_field,source_value)
                   VALUES(?,?,?,?,?,'previous_action',?,?)""",
                (game_id, int(from_event[0]), int(target_event[0]) if target_event else None,
                 from_occurrence_id, int(target_occurrence[0]) if target_occurrence else None, source_field, source_value),
            )
            created += 1
    return created


def promote_import_candidate(connection: sqlite3.Connection, import_run_id: str) -> dict[str, int | str]:
    phase = "promotion"
    candidate = None
    game_id = "unknown"
    try:
        connection.execute("BEGIN IMMEDIATE")
        candidate = connection.execute(
            "SELECT * FROM source_import_analytical_candidates WHERE import_run_id=?",
            (import_run_id,),
        ).fetchone()
        if candidate is None:
            raise ValueError(f"Candidate import run not found: {import_run_id}")
        if candidate["status"] not in {"ready_for_reconstruction", "reconstruction_failed", "promotion_failed"}:
            raise ValueError(f"Candidate {import_run_id} is not promotable from status {candidate['status']}")
        game_id = str(candidate["game_id"])
        payload = json.loads(candidate["candidate_json"])
        newer_promoted = connection.execute(
            """SELECT candidate_id FROM source_import_analytical_candidates
               WHERE game_id=? AND status='promoted' ORDER BY candidate_id DESC LIMIT 1""",
            (game_id,),
        ).fetchone()
        if newer_promoted and int(newer_promoted[0]) > int(candidate["candidate_id"]):
            raise ValueError("A newer candidate is already active; refusing to activate stale candidate")
        if identity_state_sha256(connection) != payload.get("identity_base_sha256"):
            raise ValueError("Identity tables changed after candidate resolution; stage a fresh candidate before promotion")

        apply_candidate_identity_changes(connection, payload.get("identity_changes", {}))
        decision_rows = payload.get("identity_decisions", [])
        if not decision_rows or any(row.get("status") != "resolved" or row.get("arc_player_id") is None for row in decision_rows):
            raise ValueError("Candidate identity decisions are incomplete and cannot be promoted")
        normalization_id = int(candidate["normalization_id"])
        decision_hash = str(payload["decision_set_sha256"])
        resolver_version = str(payload["resolver_version"])
        decision_set = connection.execute(
            """SELECT decision_set_id,status FROM identity_decision_sets
               WHERE normalization_id=? AND decision_set_sha256=?""",
            (normalization_id, decision_hash),
        ).fetchone()
        if decision_set and decision_set["status"] == "complete":
            decision_set_id = int(decision_set["decision_set_id"])
            existing_count = connection.execute(
                "SELECT COUNT(*) FROM identity_decisions WHERE decision_set_id=?",
                (decision_set_id,),
            ).fetchone()[0]
            if int(existing_count) != len(decision_rows):
                raise ValueError("Existing matching decision set is incomplete")
        else:
            version = connection.execute(
                "SELECT COALESCE(MAX(decision_set_version),0)+1 FROM identity_decision_sets WHERE normalization_id=?",
                (normalization_id,),
            ).fetchone()[0]
            decision_set_id = int(connection.execute(
                """INSERT INTO identity_decision_sets
                   (normalization_id,decision_set_version,resolver_version,decision_set_sha256,status)
                   VALUES(?,?,?,?, 'complete')""",
                (normalization_id, version, resolver_version, decision_hash),
            ).lastrowid)
            for row in decision_rows:
                connection.execute(
                    """INSERT INTO identity_decisions
                       (decision_set_id,normalization_id,player_occurrence_id,arc_player_id,status,decision_source,evidence_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (decision_set_id, normalization_id, int(row["player_occurrence_id"]),
                     int(row["arc_player_id"]), row["status"], row["decision_source"], row["evidence_json"]),
                )
        decision_ids_by_occurrence = {
            int(row[0]): int(row[1]) for row in connection.execute(
                "SELECT player_occurrence_id,identity_decision_id FROM identity_decisions WHERE decision_set_id=?",
                (decision_set_id,),
            )
        }
        source_occurrences_count = connection.execute(
            "SELECT COUNT(*) FROM source_player_occurrences WHERE normalization_id=?",
            (normalization_id,),
        ).fetchone()[0]
        if int(source_occurrences_count) != len(decision_ids_by_occurrence):
            raise ValueError("Candidate decision set does not cover every player occurrence")

        projection = payload["projection"]
        game_row = projection["game"]
        if str(game_row.get("game_id")) != game_id:
            raise ValueError("Candidate game metadata belongs to another game")
        game_columns = {row[1] for row in connection.execute('PRAGMA table_info("games")')}
        game_updates = {key: value for key, value in game_row.items() if key in game_columns and key != "game_id"}
        connection.execute(
            "UPDATE games SET " + ", ".join(f'"{key}"=?' for key in game_updates) + " WHERE game_id=?",
            (*game_updates.values(), game_id),
        )
        connection.execute("DELETE FROM event_relationships WHERE game_id=?", (game_id,))
        for table in ("events", "player_games", "team_games"):
            connection.execute(f'DELETE FROM "{table}" WHERE game_id=?', (game_id,))
        insert_projection_rows(connection, "team_games", projection["team_games"], game_id, decision_ids_by_occurrence)
        insert_projection_rows(connection, "player_games", projection["player_games"], game_id, decision_ids_by_occurrence)
        insert_projection_rows(connection, "events", projection["events"], game_id, decision_ids_by_occurrence)
        insert_candidate_event_relationships(connection, game_id, normalization_id)
        snapshot_id = int(candidate["snapshot_id"])
        # active_identity_decision_sets references the active source by the
        # composite (game_id, normalization_id) key. Remove the child pointer
        # before swapping its parent; both operations remain in this transaction.
        connection.execute("DELETE FROM active_identity_decision_sets WHERE game_id=?", (game_id,))
        connection.execute(
            """INSERT INTO active_game_source_snapshots(game_id,snapshot_id,normalization_id)
               VALUES(?,?,?) ON CONFLICT(game_id) DO UPDATE SET
               snapshot_id=excluded.snapshot_id, normalization_id=excluded.normalization_id""",
            (game_id, snapshot_id, normalization_id),
        )
        connection.execute(
            """INSERT INTO active_identity_decision_sets(game_id,normalization_id,decision_set_id)
               VALUES(?,?,?) ON CONFLICT(game_id) DO UPDATE SET
               normalization_id=excluded.normalization_id, decision_set_id=excluded.decision_set_id""",
            (game_id, normalization_id, decision_set_id),
        )

        prior_active = connection.execute(
            "SELECT reconstruction_run_id FROM active_reconstructions WHERE game_id=?",
            (game_id,),
        ).fetchone()
        if prior_active:
            historical_segments = connection.execute(
                "SELECT COUNT(*) FROM reconstruction_run_segments WHERE reconstruction_run_id=?",
                (prior_active[0],),
            ).fetchone()[0]
            current_segments = connection.execute(
                "SELECT COUNT(*) FROM lineup_segments WHERE game_id=?",
                (game_id,),
            ).fetchone()[0]
            if int(historical_segments) != int(current_segments):
                raise ValueError("Current active lineup projection does not match its reconstruction history")
        connection.execute("DELETE FROM lineup_segments WHERE game_id=?", (game_id,))
        connection.execute(
            "UPDATE source_import_analytical_candidates SET decision_set_id=?,status='ready_for_promotion',error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE import_run_id=?",
            (decision_set_id, import_run_id),
        )
        game_row_for_reconstruction = load_game_row_by_id(connection, game_id)
        if game_row_for_reconstruction is None:
            raise ValueError(f"Promoted game {game_id} is missing source reconstruction context")
        player_lookup = build_player_lookup(connection)
        lineup_cache = {
            (str(row[1]), row[2]): int(row[0])
            for row in connection.execute("SELECT lineup_id,team_id,lineup_hash FROM lineups")
        }
        lineup_segment_cache: dict[str, int] = {}
        phase = "reconstruction"
        result = process_game(connection, game_row_for_reconstruction, player_lookup, lineup_cache, lineup_segment_cache)
        phase = "promotion"
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"Promotion produced foreign-key violations: {len(violations)}")
        connection.execute(
            "UPDATE source_import_analytical_candidates SET status='promoted',error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE import_run_id=?",
            (import_run_id,),
        )
        connection.execute(
            "UPDATE source_import_runs SET status='imported',completed_at=CURRENT_TIMESTAMP,error_message=NULL WHERE import_run_id=?",
            (import_run_id,),
        )
        connection.commit()
        return {
            "game_id": game_id,
            "status": "promoted",
            "lineups_created": int(result["lineups_created"]),
            "lineup_segments_created": int(result["segments_created"]),
        }
    except Exception as error:
        connection.rollback()
        if candidate is not None:
            failed_status = "reconstruction_failed" if phase == "reconstruction" else "promotion_failed"
            try:
                connection.execute(
                    "UPDATE source_import_analytical_candidates SET status=?,error_message=?,updated_at=CURRENT_TIMESTAMP WHERE import_run_id=?",
                    (failed_status, str(error), import_run_id),
                )
                connection.execute(
                    "UPDATE source_import_runs SET status='failed',completed_at=CURRENT_TIMESTAMP,error_message=? WHERE import_run_id=?",
                    (str(error), import_run_id),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct lineups and lineup segments from LiveStats events")
    parser.add_argument("--database", default=str(DATABASE_PATH), help="SQLite database path")
    parser.add_argument("--debug", action="store_true", help="Run debug analysis for first invalid lineup state per failed game")
    parser.add_argument("--trace", action="store_true", help="Run single-game trace mode for lineup transitions")
    parser.add_argument("--trace-game", default="1_20222023_0008", help="Game id for --trace mode")
    parser.add_argument("--game-id", default=None, help="Reconstruct lineups for exactly one ARC game_id")
    parser.add_argument("--force", action="store_true", help="Rebuild an existing game's active projection while retaining prior run history")
    parser.add_argument("--source-snapshot-id", type=int, help="Reconstruct this persisted snapshot and activate it only on success")
    parser.add_argument("--promote-import-run-id", help="Atomically reconstruct and promote one staged analytical candidate")
    args = parser.parse_args()

    database_path = Path(args.database)

    if args.promote_import_run_id:
        promotion_connection = connect(database_path)
        try:
            ensure_duration_seconds_column(promotion_connection)
            promotion_connection.commit()
            try:
                result = promote_import_candidate(promotion_connection, args.promote_import_run_id)
            except Exception as exc:
                print(f"game_id=unknown")
                print("status=failed")
                print("lineups_created=0")
                print("lineup_segments_created=0")
                print(f"error={exc}")
                raise SystemExit(1) from exc
            print(f"game_id={result['game_id']}")
            print(f"status={result['status']}")
            print(f"lineups_created={result['lineups_created']}")
            print(f"lineup_segments_created={result['lineup_segments_created']}")
        finally:
            promotion_connection.close()
        return

    if args.trace:
        trace_connection = connect(database_path)
        try:
            run_trace_mode(trace_connection, args.trace_game)
        finally:
            trace_connection.close()
        return

    if args.debug:
        debug_connection = connect(database_path)
        try:
            run_debug_mode(debug_connection)
        finally:
            debug_connection.close()
        return

    if args.game_id:
        game_connection = connect(database_path)
        try:
            ensure_duration_seconds_column(game_connection)
            game_connection.commit()
            run_single_game_mode(game_connection, args.game_id, args.force, args.source_snapshot_id)
        finally:
            game_connection.close()
        return

    validation_connection = connect(database_path)
    try:
        ensure_duration_seconds_column(validation_connection)
        validation_connection.commit()

        validation_game = load_validation_game(validation_connection)
        if validation_game is None:
            print("game_id=none")
            print("segments_created=0")
            print("unique_lineups_created=0")
            print("validation=passed")
            print("games_processed=0")
            print("games_succeeded=0")
            print("games_failed=0")
            print("lineups_created=0")
            print("lineup_segments_created=0")
            print("runtime_errors=0")
            return

        validation_player_lookup = build_player_lookup(validation_connection)
        validation_lineup_cache: dict[tuple[str, str], int] = {}
        validation_segment_cache: dict[str, int] = {}

        while validation_game is not None:
            if validate_one_game(
                validation_connection,
                validation_game,
                validation_player_lookup,
                validation_lineup_cache,
                validation_segment_cache,
            ):
                break
            validation_game = validation_connection.execute(
                """
                SELECT g.game_id, g.home_team_id, g.away_team_id, s.json_url
                FROM games g
                JOIN source_livestats_games s ON s.game_id = g.game_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM lineup_segments ls
                    WHERE ls.game_id = g.game_id
                )
                  AND g.game_id > ?
                ORDER BY g.game_id
                LIMIT 1
                """,
                (validation_game["game_id"],),
            ).fetchone()

        if validation_game is None:
            print("game_id=none")
            print("segments_created=0")
            print("unique_lineups_created=0")
            print("validation=failed")
    finally:
        validation_connection.close()

    processing_connection = connect(database_path)
    try:
        ensure_duration_seconds_column(processing_connection)
        processing_connection.commit()
        summary = process_all_games(processing_connection)
        print(f"games_processed={summary['games_processed']}")
        print(f"games_succeeded={summary['games_succeeded']}")
        print(f"games_failed={summary['games_failed']}")
        print(f"lineups_created={summary['lineups_created']}")
        print(f"lineup_segments_created={summary['lineup_segments_created']}")
        print(f"runtime_errors={summary['runtime_errors']}")
    finally:
        processing_connection.close()


if __name__ == "__main__":
    main()
