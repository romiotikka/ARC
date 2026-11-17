import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { gunzipSync, gzipSync } from "node:zlib";
import { DeadLetterQueue, initializeDatabase } from "../lib/estlatbl-utils.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = join(__dirname, "..", "..");
const databasePath = process.env.ARC_DATABASE_PATH ?? join(rootDir, "data", "arc2.db");
const identityBridgePath = join(rootDir, "scripts", "identity", "resolve_livestats_players.py");
const lineupReconstructorPath = join(rootDir, "scripts", "core", "lineup_reconstructor.py");
const identityPython = process.env.ARC_PYTHON ?? process.env.PYTHON ?? "python";

function parseArgs(argv) {
  const args = {
    limit: 1,
    matchId: null,
    eventsOnly: false,
    recaptureSource: false,
    reprocessIdentities: false,
    retryCandidate: null,
  };

  for (let index = 2; index < argv.length; index += 1) {
    const arg = argv[index];

    if (arg === "--all") {
      args.limit = null;
    } else if (arg === "--limit") {
      args.limit = Number(argv[index + 1]);
      index += 1;
    } else if (arg === "--match-id") {
      args.matchId = argv[index + 1];
      index += 1;
    } else if (arg === "--events-only") {
      args.eventsOnly = true;
      args.limit = null;
    } else if (arg === "--recapture-source") {
      args.recaptureSource = true;
    } else if (arg === "--reprocess-identities") {
      args.reprocessIdentities = true;
    } else if (arg === "--retry-candidate") {
      args.retryCandidate = argv[index + 1];
      index += 1;
    }
  }

  return args;
}

function ensurePhase2Columns(database) {
  const additions = {
    source_event_occurrences: {
      primary_identity_status: "TEXT",
      secondary_identity_status: "TEXT",
      previous_action_number: "TEXT",
      provider_shot_previous_action: "TEXT",
      provider_shot_result: "INTEGER",
      provider_shot_x: "REAL",
      provider_shot_y: "REAL",
      provider_shot_player_ref: "TEXT",
    },
    team_games: {
      source_team_occurrence_id: "INTEGER REFERENCES source_team_occurrences(team_occurrence_id)",
      period_1_points: "INTEGER", period_2_points: "INTEGER", period_3_points: "INTEGER", period_4_points: "INTEGER", overtime_points: "INTEGER",
      field_goals_made: "INTEGER", field_goals_attempted: "INTEGER", three_pointers_made: "INTEGER", three_pointers_attempted: "INTEGER",
      free_throws_made: "INTEGER", free_throws_attempted: "INTEGER", off_reb: "INTEGER", def_reb: "INTEGER", tot_reb: "INTEGER",
      team_rebounds: "INTEGER", team_off_reb: "INTEGER", team_def_reb: "INTEGER", assists: "INTEGER", steals: "INTEGER", blocks: "INTEGER",
      blocks_received: "INTEGER", turnovers: "INTEGER", team_turnovers: "INTEGER", personal_fouls: "INTEGER", fouls_drawn: "INTEGER",
      team_fouls: "INTEGER", total_fouls: "INTEGER", points_fast_break: "INTEGER", points_from_turnovers: "INTEGER",
      points_in_paint: "INTEGER", points_second_chance: "INTEGER", bench_points: "INTEGER", biggest_lead: "INTEGER",
      biggest_scoring_run: "INTEGER", lead_changes: "INTEGER", times_scores_level: "INTEGER",
    },
    player_games: {
      source_player_occurrence_id: "INTEGER REFERENCES source_player_occurrences(player_occurrence_id)",
      identity_decision_id: "INTEGER REFERENCES identity_decisions(identity_decision_id)",
      points_fast_break: "INTEGER", points_in_paint: "INTEGER", points_second_chance: "INTEGER",
      blocks_received: "INTEGER", personal_fouls: "INTEGER", fouls_drawn: "INTEGER", captain: "INTEGER",
    },
    events: {
      source_event_occurrence_id: "INTEGER REFERENCES source_event_occurrences(event_occurrence_id)",
      primary_source_player_occurrence_id: "INTEGER REFERENCES source_player_occurrences(player_occurrence_id)",
      secondary_source_player_occurrence_id: "INTEGER REFERENCES source_player_occurrences(player_occurrence_id)",
      primary_identity_decision_id: "INTEGER REFERENCES identity_decisions(identity_decision_id)",
      secondary_identity_decision_id: "INTEGER REFERENCES identity_decisions(identity_decision_id)",
      coordinate_system: "TEXT",
    },
  };
  for (const [table, columns] of Object.entries(additions)) {
    const present = new Set(database.prepare(`PRAGMA table_info(${table})`).all().map((row) => row.name));
    for (const [column, definition] of Object.entries(columns)) {
      if (!present.has(column)) database.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
    }
  }
}

function toInteger(value) {
  if (value === undefined || value === null || value === "") {
    return null;
  }

  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function toReal(value) {
  if (value === undefined || value === null || value === "") {
    return null;
  }

  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizeText(value) {
  if (value === undefined || value === null) {
    return null;
  }

  const text = String(value).trim();
  return text.length > 0 ? text : null;
}

function normalizeClock(value) {
  const clock = normalizeText(value);
  if (!clock) {
    return null;
  }

  if (/^\d{2}:\d{2}$/.test(clock)) {
    return `00:${clock}`;
  }

  return clock;
}

function logicalPeriodNumber(action) {
  const period = toInteger(action.period) ?? 0;
  const periodType = normalizeText(action.periodType)?.toUpperCase() ?? "REGULAR";
  if (["OVERTIME", "OT"].includes(periodType) && period >= 1 && period <= 4) {
    return period <= 2 ? 4 + period : period;
  }
  return period;
}

function clockOrderValue(value) {
  const clock = normalizeText(value);
  if (!clock) return null;
  const parts = clock.split(":");
  if (![2, 3].includes(parts.length) || !parts.every((part) => /^\d+$/.test(part))) {
    return null;
  }
  const [minutes, seconds, subsecond = 0] = parts.map(Number);
  return minutes * 6000 + seconds * 100 + subsecond;
}

function parseJsonArray(value) {
  if (Array.isArray(value)) {
    return value;
  }

  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  return [];
}

function normalizePlayerKey(value) {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/(\p{Script=Latin})\p{M}+/gu, "$1")
    .replace(/[^\p{L}\p{N}\p{M}]+/gu, "")
    .toLowerCase()
    .trim();
}

let playerGamesColumnsCache = null;

function occurrenceKey(
  teamNumber,
  playerName,
  externalPlayerId = null,
  jerseyNumber = null,
  rosterEntryId = null,
) {
  return `${teamNumber}|${normalizePlayerKey(playerName)}|${externalPlayerId ?? ""}|${jerseyNumber ?? ""}|${rosterEntryId ?? ""}`;
}

function resolveLiveStatsPlayerOccurrences(mapping, data, teamIdsByLiveStatsNumber, database) {
  const playerLookupByTeam = buildLiveStatsPlayerLookup(data);
  const occurrences = new Map();
  const rosterKeysByIdentifier = {
    providerPlayerId: new Map(),
    shirtNumber: new Map(),
  };
  const rosterOccurrenceKeys = new WeakMap();

  const addOccurrence = (teamNumber, playerName, details = {}) => {
    const rawName = normalizeText(playerName);
    const teamId = teamIdsByLiveStatsNumber.get(String(teamNumber));
    if (!rawName || !teamId) return null;

    const key = occurrenceKey(
      teamNumber,
      rawName,
      details.externalPlayerId,
      details.jerseyNumber,
      details.rosterEntryId,
    );
    const sourceTeam = data.tm?.[String(teamNumber)] ?? {};
    if (!occurrences.has(key)) {
      occurrences.set(key, {
        key,
        raw_name: rawName,
        team_id: teamId,
        season_id: mapping.season_id,
        league_id: mapping.league_id,
        game_id: mapping.game_id,
        provider: "fiba_livestats",
        provider_team_external_id: providerTeamId(sourceTeam),
        jersey_number: normalizeText(details.jerseyNumber),
        position: normalizeText(details.position),
        external_player_id: normalizeText(details.externalPlayerId),
        canonical_name_hint: normalizeText(details.canonicalNameHint),
        first_name: normalizeText(details.firstName),
        last_name: normalizeText(details.lastName),
        ambiguous_source_name_group: details.ambiguousSourceNameGroup === true,
      });
    }
    return key;
  };

  const addRosterIndex = (index, key, occurrenceKeyValue) => {
    if (!key) return;
    if (!index.has(key)) index.set(key, new Set());
    index.get(key).add(occurrenceKeyValue);
  };

  const ambiguousRosterPlayers = new WeakSet();
  for (const team of Object.values(data.tm ?? {})) {
    const playersByName = new Map();
    for (const player of Object.values(team.pl ?? {})) {
      const nameKey = normalizePlayerKey(player.name ?? player.scoreboardName);
      if (!nameKey) continue;
      if (!playersByName.has(nameKey)) playersByName.set(nameKey, []);
      playersByName.get(nameKey).push(player);
    }
    for (const players of playersByName.values()) {
      if (players.length < 2) continue;
      const fullNames = players.map(buildCanonicalPlayerName);
      const sourceIds = players.map((player) => normalizeText(player.playerId));
      const distinctFullNames = fullNames.every(Boolean) && new Set(fullNames.map(normalizePlayerKey)).size === players.length;
      const distinctSourceIds = sourceIds.every(Boolean) && new Set(sourceIds).size === players.length;
      if (distinctFullNames || distinctSourceIds) continue;
      for (const player of players) ambiguousRosterPlayers.add(player);
    }
  }

  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    for (const [rosterEntryId, player] of Object.entries(team.pl ?? {})) {
      const rawName = normalizeText(player.name ?? player.scoreboardName);
      const key = addOccurrence(teamNumber, player.name ?? player.scoreboardName, {
        externalPlayerId: player.playerId,
        jerseyNumber: player.shirtNumber,
        position: player.playingPosition,
        rosterEntryId,
        canonicalNameHint: buildCanonicalPlayerName(player),
        firstName: player.internationalFirstName ?? player.firstName,
        lastName: player.internationalFamilyName ?? player.familyName,
        ambiguousSourceNameGroup: ambiguousRosterPlayers.has(player),
      });
      if (key) {
        rosterOccurrenceKeys.set(player, key);
        for (const [identifierType, identifier] of [
          ["providerPlayerId", player.playerId],
          ["shirtNumber", player.shirtNumber],
        ]) {
          const normalizedIdentifier = normalizeText(identifier);
          if (normalizedIdentifier) addRosterIndex(
            rosterKeysByIdentifier[identifierType],
            `${teamNumber}|${normalizedIdentifier}`,
            key,
          );
        }
      }
    }
  }

  const eventKeys = new WeakMap();
  const eventIdentityInputs = [];
  const resolvedNameKeysByOccurrence = new Map();
  const rosterKeysForEvent = (teamNumber, playerName, evidence = {}) => {
    const team = data.tm?.[String(teamNumber)];
    const roster = team?.pl ?? {};
    const allKeys = new Set(Object.values(roster).map((player) => rosterOccurrenceKeys.get(player)).filter(Boolean));
    const nameKey = normalizePlayerKey(playerName);
    const nameMatches = new Set();
    if (nameKey) {
      for (const player of Object.values(roster)) {
        const key = rosterOccurrenceKeys.get(player);
        const providerKeys = providerNameVariants(player).map(normalizePlayerKey);
        if (key && (providerKeys.includes(nameKey) || resolvedNameKeysByOccurrence.get(key)?.has(nameKey))) nameMatches.add(key);
      }
    }
    const id = normalizeText(evidence.providerPlayerId);
    const jersey = normalizeText(evidence.shirtNumber);
    const idMatches = id ? new Set(rosterKeysByIdentifier.providerPlayerId.get(`${teamNumber}|${id}`) ?? []) : null;
    const jerseyMatches = jersey ? new Set(rosterKeysByIdentifier.shirtNumber.get(`${teamNumber}|${jersey}`) ?? []) : null;
    const otherTeamHasId = id && Object.entries(data.tm ?? {}).some(([otherTeam, other]) =>
      otherTeam !== String(teamNumber) && Object.values(other.pl ?? {}).some((player) => normalizeText(player.playerId) === id));
    const evidenceSets = [nameKey ? nameMatches : null, idMatches, jerseyMatches].filter(Boolean);
    if (!evidenceSets.length) return { key: null, status: "unresolved" };
    if (evidenceSets.some((set) => set.size === 0)) {
      return { key: null, status: otherTeamHasId || evidenceSets.some((set) => set.size > 0) ? "conflict" : "unresolved" };
    }
    const matching = [...allKeys].filter((key) => evidenceSets.every((set) => set.has(key)));
    if (matching.length === 1) return { key: matching[0], status: "resolved" };
    return { key: null, status: matching.length ? "ambiguous" : "conflict" };
  };

  for (const action of parseJsonArray(data.pbp)) {
    const teamNumber = String(action.tno ?? "");
    for (const [role, playerName, evidence] of [
      ["primary", resolvePlayerNameFromAction(action, playerLookupByTeam), { providerPlayerId: action.playerId, pno: action.pno, shirtNumber: action.shirtNumber }],
      ["secondary", resolveSecondaryPlayerName(action, playerLookupByTeam), secondaryIdentityEvidence(action)],
    ]) {
      const rawName = normalizeText(playerName);
      if (!rawName && !normalizeText(evidence.providerPlayerId) && !normalizeText(evidence.shirtNumber) && !normalizeText(evidence.pno)) continue;
      eventIdentityInputs.push({ action, teamNumber, role, rawName, evidence });
    }
  }

  const additionalTeams = [...new Set(teamIdsByLiveStatsNumber.values())].map((teamId) => {
    const team = database.prepare("SELECT team_id, canonical_name FROM teams WHERE team_id = ?").get(teamId);
    if (!team) throw new Error(`Missing ARC team ${teamId} while staging identity resolution`);
    return team;
  });
  const process = spawnSync(identityPython, [identityBridgePath, "--database", databasePath], {
    input: JSON.stringify({ occurrences: [...occurrences.values()], additional_teams: additionalTeams }),
    encoding: "utf8",
    windowsHide: true,
  });
  if (process.error) {
    throw new Error(`IdentityResolver bridge could not start (${identityPython}): ${process.error.message}`);
  }
  if (process.status !== 0) {
    throw new Error(`IdentityResolver bridge failed: ${process.stderr.trim() || process.stdout.trim()}`);
  }

  let output;
  try {
    output = JSON.parse(process.stdout);
  } catch (error) {
    throw new Error(`IdentityResolver bridge returned invalid JSON: ${error.message}`);
  }

  const resolved = output.resolved ?? {};
  const reviewsByKey = new Map((output.reviews ?? []).map((review) => [review.key, review]));
  const stagedNamesByPlayer = new Map();
  const addStagedName = (playerId, name) => {
    const normalized = normalizePlayerKey(name);
    if (!normalized) return;
    if (!stagedNamesByPlayer.has(String(playerId))) stagedNamesByPlayer.set(String(playerId), new Set());
    stagedNamesByPlayer.get(String(playerId)).add(normalized);
  };
  for (const player of output.identity_changes?.players ?? []) addStagedName(player.player_id, player.canonical_name);
  for (const alias of output.identity_changes?.player_aliases ?? []) addStagedName(alias.player_id, alias.alias_name);
  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    for (const player of Object.values(team.pl ?? {})) {
      const occurrence = rosterOccurrenceKeys.get(player);
      const playerId = occurrence ? resolved[occurrence]?.player_id : null;
      if (!occurrence || !playerId) continue;
      const names = new Set(providerNameVariants(player).map(normalizePlayerKey).filter(Boolean));
      for (const row of database.prepare("SELECT canonical_name FROM players WHERE player_id = ?").all(playerId)) {
        const key = normalizePlayerKey(row.canonical_name);
        if (key) names.add(key);
      }
      for (const row of database.prepare("SELECT alias_name FROM player_aliases WHERE player_id = ?").all(playerId)) {
        const key = normalizePlayerKey(row.alias_name);
        if (key) names.add(key);
      }
      for (const key of stagedNamesByPlayer.get(String(playerId)) ?? []) names.add(key);
      resolvedNameKeysByOccurrence.set(occurrence, names);
    }
  }
  for (const { action, teamNumber, role, rawName, evidence } of eventIdentityInputs) {
    if (!eventKeys.has(action)) eventKeys.set(action, new Map());
    eventKeys.get(action).set(role, rosterKeysForEvent(teamNumber, rawName, evidence));
  }
  const playerIdForKey = (key) => resolved[key]?.player_id ?? null;

  return {
    identityChanges: output.identity_changes ?? {},
    identityBaseSha256: output.identity_base_sha256 ?? null,
    reviews: output.reviews ?? [],
    playerIdForRoster(teamNumber, player) {
      const occurrence = rosterOccurrenceKeys.get(player);
      if (occurrence) return playerIdForKey(occurrence);
      const name = player.name ?? player.scoreboardName;
      return playerIdForKey(
        occurrenceKey(teamNumber, name, player.playerId, player.shirtNumber),
      );
    },
    rosterOccurrence(teamNumber, player) {
      const key = rosterOccurrenceKeys.get(player) ?? occurrenceKey(
        teamNumber, player.name ?? player.scoreboardName, player.playerId,
        player.shirtNumber, null,
      );
      const review = reviewsByKey.get(key);
      return {
        key,
        identity: resolved[key] ?? (review ? {
          player_id: null,
          status: "review",
          confidence: null,
          evidence: review,
        } : null),
      };
    },
    eventOccurrenceKey(action, role = "primary") {
      return eventKeys.get(action)?.get(role)?.key ?? null;
    },
    playerIdForEvent(teamNumber, playerName, action = null, role = "primary") {
      const key = eventKeys.get(action)?.get(role)?.key;
      return key ? playerIdForKey(key) : null;
    },
    eventIdentityStatus(action, role = "primary") {
      const result = eventKeys.get(action)?.get(role);
      if (!result) return "not_applicable";
      if (result.key && reviewsByKey.has(result.key)) return "review";
      if (result.key && !resolved[result.key]?.player_id) return "unresolved";
      return result.status;
    },
  };
}

function parseIdentityReview(stderr) {
  const marker = "ARC_IDENTITY_REVIEW ";
  const line = stderr
    .split(/\r?\n/)
    .find((entry) => entry.startsWith(marker));
  if (!line) return null;

  try {
    return JSON.parse(line.slice(marker.length));
  } catch {
    return null;
  }
}

const identityChangeTables = {
  players: ["player_id"],
  player_aliases: ["alias_id"],
  player_external_ids: ["player_id", "provider", "external_player_id"],
  team_external_ids: ["team_id", "provider"],
  season_external_ids: ["season_id", "provider"],
  league_external_ids: ["league_id", "provider"],
};

function applyIdentityChanges(database, changes) {
  for (const [table, primaryKey] of Object.entries(identityChangeTables)) {
    const rows = changes?.[table] ?? [];
    if (!Array.isArray(rows)) {
      throw new Error(`Identity bridge returned invalid ${table} changes`);
    }
    for (const row of rows) {
      const columns = Object.keys(row);
      if (!primaryKey.every((column) => columns.includes(column))) {
        throw new Error(`Identity bridge ${table} change is missing its primary key`);
      }
      const quotedColumns = columns.map((column) => `"${column}"`).join(", ");
      const placeholders = columns.map(() => "?").join(", ");
      const conflictColumns = primaryKey.map((column) => `"${column}"`).join(", ");
      const updates = columns
        .filter((column) => !primaryKey.includes(column))
        .map((column) => `"${column}" = excluded."${column}"`);
      const sql = `INSERT INTO "${table}" (${quotedColumns}) VALUES (${placeholders}) ON CONFLICT (${conflictColumns}) DO ${updates.length ? `UPDATE SET ${updates.join(", ")}` : "NOTHING"}`;
      database.prepare(sql).run(...columns.map((column) => row[column]));
    }
  }
}

function identityStateSha256(database) {
  const tables = ["players", "player_aliases", "player_external_ids", "team_external_ids", "season_external_ids", "league_external_ids"];
  const state = {};
  for (const table of tables) {
    state[table] = database.prepare(`SELECT * FROM "${table}"`).all()
      .map((row) => Object.fromEntries(Object.entries(row)))
      .sort((left, right) => stableJson(left).localeCompare(stableJson(right)));
  }
  return sha256(Buffer.from(stableJson(state), "utf8"));
}

function buildLiveStatsPlayerLookup(data) {
  const lookup = new Map();

  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    const teamLookup = { providerPlayerId: new Map(), shirtNumber: new Map(), players: [] };

    for (const [playerKey, player] of Object.entries(team.pl ?? {})) {
      const record = { rosterEntryId: String(playerKey), player, name: normalizeText(player.name ?? player.scoreboardName) };
      teamLookup.players.push(record);
      for (const [namespace, value] of [["providerPlayerId", player.playerId], ["shirtNumber", player.shirtNumber]]) {
        const normalized = normalizeText(value);
        if (!normalized) continue;
        if (!teamLookup[namespace].has(normalized)) teamLookup[namespace].set(normalized, []);
        teamLookup[namespace].get(normalized).push(record);
      }
    }

    lookup.set(String(teamNumber), teamLookup);
  }

  return lookup;
}

function resolvePlayerNameFromAction(action, playerLookupByTeam) {
  const explicit = normalizeText(action.player);
  if (explicit) {
    return explicit;
  }

  const teamLookup = playerLookupByTeam.get(String(action.tno));
  const ids = teamLookup?.providerPlayerId.get(normalizeText(action.playerId)) ?? [];
  const jerseys = teamLookup?.shirtNumber.get(normalizeText(action.shirtNumber)) ?? [];
  const candidates = ids.length ? ids : jerseys;
  return candidates.length === 1 ? candidates[0].name : null;
}

function secondaryIdentityEvidence(action) {
  const first = (fields) => fields.map((field) => normalizeText(action[field])).find(Boolean) ?? null;
  return {
    providerPlayerId: first(["secondaryPlayerId", "secondary_player_id", "playerId2", "relatedPlayerId", "assistPlayerId", "stealPlayerId", "blockPlayerId"]),
    pno: first(["secondaryPno", "secondary_pno", "pno2", "relatedPno", "assistPno", "stealPno", "blockPno", "drawnByPno", "fouledPno", "defenderPno"]),
    shirtNumber: first(["secondaryShirtNumber", "secondary_shirt_number", "shirtNumber2", "relatedShirtNumber"]),
  };
}

function resolveSecondaryPlayerName(action, playerLookupByTeam) {
  const directNameFields = [
    "secondaryPlayer",
    "secondary_player",
    "otherPlayer",
    "relatedPlayer",
    "player2",
    "assistPlayer",
    "stealPlayer",
    "blockPlayer",
    "drawnBy",
    "fouledPlayer",
    "defender",
    "receiver",
    "passer",
    "substitute",
    "replacedBy",
  ];

  for (const field of directNameFields) {
    const value = normalizeText(action[field]);
    if (value) {
      return value;
    }
  }

  const teamLookup = playerLookupByTeam.get(String(action.tno));
  if (!teamLookup) {
    return null;
  }

  const evidence = secondaryIdentityEvidence(action);
  const byId = teamLookup.providerPlayerId.get(normalizeText(evidence.providerPlayerId)) ?? [];
  if (byId.length === 1) return byId[0].name;
  const byJersey = teamLookup.shirtNumber.get(normalizeText(evidence.shirtNumber)) ?? [];
  if (byJersey.length === 1) return byJersey[0].name;

  return null;
}

function extractCoordinates(action) {
  const x =
    toReal(action.x) ??
    toReal(action.coordX) ??
    toReal(action.locX) ??
    toReal(action.shotX) ??
    toReal(action.shotLocationX);
  const y =
    toReal(action.y) ??
    toReal(action.coordY) ??
    toReal(action.locY) ??
    toReal(action.shotY) ??
    toReal(action.shotLocationY);

  if (x === null || y === null) {
    return { x: null, y: null, distance: null };
  }

  // LiveStats coordinates are provider-native. Without a verified basket origin
  // and scale, distance from (0, 0) is not a basketball shot distance.
  return { x, y, distance: null };
}

function indexLiveStatsShotObjects(data) {
  const pbp = parseJsonArray(data.pbp);
  const actionsByNumber = new Map();
  for (const action of pbp) {
    const actionNumber = toInteger(action.actionNumber);
    if (actionNumber === null) continue;
    if (actionsByNumber.has(actionNumber)) throw new Error(`Duplicate LiveStats actionNumber ${actionNumber}`);
    actionsByNumber.set(actionNumber, action);
  }

  const shotObjectsByActionNumber = new Map();
  let hasShotCollection = false;
  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    if (team.shot === undefined || team.shot === null) continue;
    if (!Array.isArray(team.shot)) throw new Error(`INVALID_LIVESTATS_SHOT_COLLECTION: team ${teamNumber}`);
    hasShotCollection = true;
    for (const shot of team.shot) {
      const actionNumber = toInteger(shot.actionNumber);
      if (actionNumber === null) throw new Error("SHOT_OBJECT_MISSING_ACTION_NUMBER");
      if (shotObjectsByActionNumber.has(actionNumber)) throw new Error(`DUPLICATE_LIVESTATS_SHOT_ACTION_NUMBER: ${actionNumber}`);
      const action = actionsByNumber.get(actionNumber);
      if (!action || !["2pt", "3pt"].includes(String(action.actionType ?? "").toLowerCase())) {
        throw new Error(`SHOT_OBJECT_HAS_NO_FIELD_GOAL_ACTION: ${actionNumber}`);
      }
      const comparisons = [
        [String(shot.tno), String(teamNumber), "team"],
        [String(shot.tno), String(action.tno), "event team"],
        [String(shot.actionType).toLowerCase(), String(action.actionType).toLowerCase(), "action type"],
        [String(shot.per), String(action.period), "period"],
        [String(shot.perType), String(action.periodType), "period type"],
        [String(shot.pno), String(action.pno), "player roster key"],
        [String(shot.shirtNumber), String(action.shirtNumber), "jersey"],
        [String(shot.player), String(action.player), "player name"],
      ];
      for (const [shotValue, actionValue, field] of comparisons) {
        if (shotValue !== actionValue) throw new Error(`SHOT_OBJECT_${field.toUpperCase().replaceAll(" ", "_")}_MISMATCH: ${actionNumber}`);
      }
      const result = toInteger(shot.r);
      const actionSuccess = toInteger(action.success);
      if (![0, 1].includes(result) || result !== actionSuccess) {
        throw new Error(`SHOT_OBJECT_RESULT_MISMATCH: ${actionNumber}`);
      }
      const x = toReal(shot.x);
      const y = toReal(shot.y);
      if ((shot.x !== undefined && x === null) || (shot.y !== undefined && y === null)) {
        throw new Error(`INVALID_SHOT_OBJECT_COORDINATES: ${actionNumber}`);
      }
      shotObjectsByActionNumber.set(actionNumber, { ...shot, actionNumber, result, x, y });
    }
  }

  if (hasShotCollection) {
    for (const [actionNumber, action] of actionsByNumber) {
      if (["2pt", "3pt"].includes(String(action.actionType ?? "").toLowerCase()) && !shotObjectsByActionNumber.has(actionNumber)) {
        throw new Error(`PBP_FIELD_GOAL_MISSING_SHOT_OBJECT: ${actionNumber}`);
      }
    }
  }
  return shotObjectsByActionNumber;
}

function getEventResult(actionType, subType, success) {
  if (["2pt", "3pt", "freethrow"].includes(actionType)) {
    if (success === 1) {
      return "made";
    }
    if (success === 0) {
      return "missed";
    }
  }

  if (actionType === "jumpball" && subType) {
    return subType;
  }

  if (success === 1) {
    return "success";
  }
  if (success === 0) {
    return "failed";
  }

  return null;
}

function isLastFreeThrowAttempt(subType) {
  const parsed = String(subType ?? "").match(/^(\d+)of(\d+)$/i);
  if (!parsed) {
    return false;
  }

  return Number(parsed[1]) === Number(parsed[2]);
}

function nextPossessionTeamId(actionType, subType, success, teamId, opponentTeamId, currentPossessionTeamId) {
  if (!teamId && actionType !== "period" && actionType !== "game") {
    return currentPossessionTeamId;
  }

  if (actionType === "jumpball") {
    if (subType === "won" || subType === "startperiod") {
      return teamId;
    }
    if (subType === "lost") {
      return opponentTeamId ?? currentPossessionTeamId;
    }
  }

  if (actionType === "rebound") {
    return teamId ?? currentPossessionTeamId;
  }

  if (actionType === "turnover") {
    return opponentTeamId ?? currentPossessionTeamId;
  }

  if (actionType === "steal") {
    return teamId ?? currentPossessionTeamId;
  }

  if (actionType === "foul" && subType === "offensive") {
    return opponentTeamId ?? currentPossessionTeamId;
  }

  if ((actionType === "2pt" || actionType === "3pt") && success === 1) {
    return opponentTeamId ?? currentPossessionTeamId;
  }

  if (actionType === "freethrow" && success === 1 && isLastFreeThrowAttempt(subType)) {
    return opponentTeamId ?? currentPossessionTeamId;
  }

  if (actionType === "period" && subType === "start") {
    return null;
  }

  if (actionType === "period" && subType === "end") {
    return null;
  }

  if (actionType === "game" && subType === "start") {
    return null;
  }

  if (actionType === "game" && subType === "end") {
    return null;
  }

  return currentPossessionTeamId;
}

function eventMetadata(action, identityStatus = null) {
  const mappedKeys = new Set([
    "actionNumber",
    "period",
    "clock",
    "actionType",
    "success",
    "subType",
    "x",
    "y",
    "coordX",
    "coordY",
    "locX",
    "locY",
    "shotX",
    "shotY",
    "shotLocationX",
    "shotLocationY",
  ]);

  const metadata = {};
  for (const [key, value] of Object.entries(action ?? {}).sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))) {
    if (mappedKeys.has(key)) {
      continue;
    }

    metadata[key] = value;
  }

  if (identityStatus) metadata.arcIdentityResolution = identityStatus;

  return Object.keys(metadata).length > 0 ? JSON.stringify(metadata) : null;
}

function importEvents(database, gameId, data, teamIdsByLiveStatsNumber, playerResolver, sourceContext) {
  const pbp = parseJsonArray(data.pbp);
  const playerLookupByTeam = buildLiveStatsPlayerLookup(data);

  database.prepare("DELETE FROM events WHERE game_id = ?").run(gameId);

  const insertEvent = database.prepare(
    `
    INSERT INTO events (
      game_id,
      action_number,
      period,
      clock,
      team_id,
      player_id,
      event_secondary_player_id,
      event_type,
      event_result,
      shot_type,
      shot_move,
      points,
      x,
      y,
      coordinate_system,
      distance,
      possession_team_id_after,
      shot_clock,
      contested,
      metadata_json,
      source_event_occurrence_id,
      primary_source_player_occurrence_id,
      secondary_source_player_occurrence_id,
      primary_identity_decision_id,
      secondary_identity_decision_id
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (game_id, action_number) DO UPDATE SET
      period = excluded.period,
      clock = excluded.clock,
      team_id = excluded.team_id,
      player_id = excluded.player_id,
      event_secondary_player_id = excluded.event_secondary_player_id,
      event_type = excluded.event_type,
      event_result = excluded.event_result,
      shot_type = excluded.shot_type,
      shot_move = excluded.shot_move,
      points = excluded.points,
      x = excluded.x,
      y = excluded.y,
      coordinate_system = excluded.coordinate_system,
      distance = excluded.distance,
      possession_team_id_after = excluded.possession_team_id_after,
      shot_clock = excluded.shot_clock,
      contested = excluded.contested,
      metadata_json = excluded.metadata_json,
      source_event_occurrence_id = excluded.source_event_occurrence_id,
      primary_source_player_occurrence_id = excluded.primary_source_player_occurrence_id,
      secondary_source_player_occurrence_id = excluded.secondary_source_player_occurrence_id,
      primary_identity_decision_id = excluded.primary_identity_decision_id,
      secondary_identity_decision_id = excluded.secondary_identity_decision_id
    `,
  );

  const normalizedEvents = pbp.map((action, index) => {
    const actionType = normalizeText(action.actionType)?.toLowerCase() ?? "unknown";
    const subType = normalizeText(action.subType)?.toLowerCase() ?? null;
    const shotObject = sourceContext.shotObjectsByActionNumber.get(toInteger(action.actionNumber));
    const success = toInteger(action.success) ?? shotObject?.result ?? null;
    const teamId = teamIdsByLiveStatsNumber.get(String(action.tno)) ?? null;
    const opponentTeamId = teamId
      ? [...teamIdsByLiveStatsNumber.values()].find((value) => value !== teamId) ?? null
      : null;

    const playerName = resolvePlayerNameFromAction(action, playerLookupByTeam);
    const secondaryName = resolveSecondaryPlayerName(action, playerLookupByTeam);

    const playerId = playerResolver.playerIdForEvent(String(action.tno ?? ""), playerName, action, "primary");
    const secondaryPlayerId = playerResolver.playerIdForEvent(String(action.tno ?? ""), secondaryName, action, "secondary");

    const shotType = ["2pt", "3pt", "freethrow"].includes(actionType) ? actionType : null;
    const shotMove = shotType ? subType : null;
    const providerCoordinates = shotObject
      ? { x: shotObject.x, y: shotObject.y, distance: null }
      : extractCoordinates(action);
    const { x, y, distance } = providerCoordinates;
    const points = shotType
      ? success === 1 ? (actionType === "2pt" ? 2 : actionType === "3pt" ? 3 : 1) : success === 0 ? 0 : null
      : null;

    return {
      index,
      gameId,
      actionNumber: toInteger(action.actionNumber) ?? index + 1,
      sourceActionNumber: toInteger(action.actionNumber),
      period: toInteger(action.period),
      clock: normalizeClock(action.clock),
      clockOrderValue: clockOrderValue(action.clock),
      teamId,
      opponentTeamId,
      playerId,
      secondaryPlayerId,
      eventType: actionType,
      eventResult: getEventResult(actionType, subType, success),
      shotType,
      shotMove,
      points,
      x,
      y,
      coordinateSystem: shotObject ? "fiba_livestats_shot_object_native" : (x !== null && y !== null ? "fiba_livestats_pbp_native" : null),
      distance,
      shotClock: null,
      contested: null,
      metadataJson: eventMetadata(action, {
        primary: playerResolver.eventIdentityStatus(action, "primary"),
        secondary: playerResolver.eventIdentityStatus(action, "secondary"),
      }),
      sourceEventOccurrenceId: sourceContext.eventOccurrenceIds[index] ?? null,
      primarySourcePlayerOccurrenceId: sourceContext.rosterOccurrenceIds.get(playerResolver.eventOccurrenceKey(action, "primary")) ?? null,
      secondarySourcePlayerOccurrenceId: sourceContext.rosterOccurrenceIds.get(playerResolver.eventOccurrenceKey(action, "secondary")) ?? null,
      primaryIdentityDecisionId: sourceContext.decisionIds.get(playerResolver.eventOccurrenceKey(action, "primary")) ?? null,
      secondaryIdentityDecisionId: sourceContext.decisionIds.get(playerResolver.eventOccurrenceKey(action, "secondary")) ?? null,
      possessionTeamIdAfter: null,
      subType,
      success,
    };
  });

  const seenActionNumbers = new Set();
  for (const event of normalizedEvents) {
    if (seenActionNumbers.has(event.actionNumber)) {
      throw new Error(`Duplicate LiveStats actionNumber ${event.actionNumber} for game ${gameId}`);
    }
    seenActionNumbers.add(event.actionNumber);
  }

  const byChronologicalOrder = [...normalizedEvents].sort((left, right) => {
    const periodDifference = logicalPeriodNumber(left) - logicalPeriodNumber(right);
    if (periodDifference !== 0) return periodDifference;

    if (left.clockOrderValue !== null && right.clockOrderValue !== null && left.clockOrderValue !== right.clockOrderValue) {
      return right.clockOrderValue - left.clockOrderValue;
    }
    if (left.clockOrderValue === null || right.clockOrderValue === null) {
      return right.index - left.index;
    }

    if (left.sourceActionNumber !== null && right.sourceActionNumber !== null && left.sourceActionNumber !== right.sourceActionNumber) {
      return left.sourceActionNumber - right.sourceActionNumber;
    }
    return right.index - left.index;
  });

  let possessionTeamId = null;
  for (const event of byChronologicalOrder) {
    possessionTeamId = nextPossessionTeamId(
      event.eventType,
      event.subType,
      event.success,
      event.teamId,
      event.opponentTeamId,
      possessionTeamId,
    );
    event.possessionTeamIdAfter = possessionTeamId;
  }

  const chronologicalByIndex = new Map(byChronologicalOrder.map((event) => [event.index, event]));
  const eventTypes = new Set();

  for (const originalEvent of normalizedEvents) {
    const event = chronologicalByIndex.get(originalEvent.index);
    eventTypes.add(event.eventType);

    insertEvent.run(
      event.gameId,
      event.actionNumber,
      event.period,
      event.clock,
      event.teamId,
      event.playerId,
      event.secondaryPlayerId,
      event.eventType,
      event.eventResult,
      event.shotType,
      event.shotMove,
      event.points,
      event.x,
      event.y,
        event.coordinateSystem,
      event.distance,
      event.possessionTeamIdAfter,
      event.shotClock,
      event.contested,
      event.metadataJson,
      event.sourceEventOccurrenceId,
      event.primarySourcePlayerOccurrenceId,
      event.secondarySourcePlayerOccurrenceId,
      event.primaryIdentityDecisionId,
      event.secondaryIdentityDecisionId,
    );
  }

  return {
    imported: normalizedEvents.length,
    eventTypesHandled: eventTypes.size,
  };
}

function buildCanonicalPlayerName(player) {
  const firstName = normalizeText(player.internationalFirstName ?? player.firstName);
  const lastName = normalizeText(player.internationalFamilyName ?? player.familyName);

  if (firstName && lastName) {
    return `${firstName} ${lastName}`;
  }

  const displayName = normalizeText(player.name ?? player.scoreboardName);
  return displayName && !displayName.includes(".") && displayName.split(/\s+/).length >= 2
    ? displayName
    : null;
}

function providerNameVariants(player) {
  const firstName = normalizeText(player.internationalFirstName ?? player.firstName);
  const lastName = normalizeText(player.internationalFamilyName ?? player.familyName);
  const values = [player.name, player.scoreboardName, buildCanonicalPlayerName(player)];
  if (firstName && lastName) {
    values.push(`${lastName} ${firstName}`, `${firstName[0]}. ${lastName}`);
  }
  for (const value of [...values]) {
    const text = normalizeText(value);
    if (text?.includes(",")) {
      const [family, ...givenParts] = text.split(",");
      const given = givenParts.join(" ").trim();
      if (family.trim() && given) values.push(`${given} ${family.trim()}`);
    }
  }
  return values.filter(Boolean);
}

function providerTeamId(team) {
  return normalizeText(team.teamId ?? team.code);
}

function selectMappings(database, args) {
  if (args.matchId) {
    return database
      .prepare(
        `
        SELECT *
        FROM source_livestats_games
        WHERE match_id = ?
        `,
      )
      .all(args.matchId);
  }

  const baseQuery = `
    SELECT s.*
    FROM source_livestats_games s
    LEFT JOIN team_games tg ON tg.game_id = s.game_id
    LEFT JOIN games g ON g.game_id = s.game_id
    WHERE tg.team_game_id IS NULL
      AND COALESCE(g.status, 'scheduled_or_import_pending') <> 'boxscore_import_failed'
      AND COALESCE(g.status, '') NOT IN ('scheduled', 'not_played')
    ORDER BY s.provider_game_id
  `;

  if (args.limit === null) {
    return database.prepare(baseQuery).all();
  }

  return database.prepare(`${baseQuery} LIMIT ?`).all(args.limit);
}

async function fetchJson(url) {
  const response = await fetch(url);

  if (!response.ok) {
    throw new Error(`LiveStats request failed: ${response.status} ${response.statusText}`);
  }

  return response.json();
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function fetchSourceBody(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`LiveStats request failed: ${response.status} ${response.statusText}`);
  const body = Buffer.from(await response.arrayBuffer());
  let data = null;
  let parseError = null;
  try { data = JSON.parse(body.toString("utf8")); } catch (error) { parseError = error; }
  return { body, data, parseError, contentType: response.headers.get("content-type"), contentEncoding: response.headers.get("content-encoding") };
}

function captureSourceSnapshot(database, mapping, body, headers = {}, captureKind = "initial_import") {
  const bodyHash = sha256(body);
  let row = database.prepare(
    "SELECT snapshot_id FROM source_game_snapshots WHERE game_id = ? AND provider = 'fiba_livestats' AND captured_body_sha256 = ?",
  ).get(mapping.game_id, bodyHash);
  if (!row) {
    const insert = database.prepare(`
      INSERT INTO source_game_snapshots
        (game_id, provider, provider_game_id, source_url, capture_kind, content_type, content_encoding, payload_gzip, captured_body_sha256)
      VALUES (?, 'fiba_livestats', ?, ?, ?, ?, ?, ?, ?)
    `);
    const result = insert.run(
      mapping.game_id, String(mapping.provider_game_id ?? ""), mapping.json_url, captureKind,
      headers.contentType ?? null, headers.contentEncoding ?? null, gzipSync(body), bodyHash,
    );
    row = { snapshot_id: Number(result.lastInsertRowid) };
  }
  return Number(row.snapshot_id);
}

function loadSnapshotData(database, snapshotId) {
  const row = database.prepare(
    "SELECT payload_gzip, captured_body_sha256 FROM source_game_snapshots WHERE snapshot_id = ?",
  ).get(snapshotId);
  if (!row) throw new Error(`SOURCE_SNAPSHOT_MISSING: snapshot ${snapshotId} does not exist`);
  const body = gunzipSync(row.payload_gzip);
  if (sha256(body) !== row.captured_body_sha256) throw new Error(`SOURCE_SNAPSHOT_HASH_MISMATCH: snapshot ${snapshotId}`);
  return JSON.parse(body.toString("utf8"));
}

function acquireSourceSnapshot(database, mapping, recapture = false) {
  const associateReuse = (snapshotId) => {
    const runId = randomUUID();
    database.prepare("INSERT INTO source_import_runs(import_run_id, provider, capture_kind, status) VALUES (?, 'fiba_livestats', 'reused_snapshot', 'captured')").run(runId);
    database.prepare("INSERT INTO source_import_run_snapshots(import_run_id, snapshot_id) VALUES (?, ?)").run(runId, snapshotId);
    return { snapshotId, data: loadSnapshotData(database, snapshotId), importRunId: runId, newlyCaptured: false };
  };
  const oldImport = database.prepare("SELECT 1 FROM team_games WHERE game_id = ? LIMIT 1").get(mapping.game_id);
  const hasPriorSnapshot = Boolean(database.prepare("SELECT 1 FROM source_game_snapshots WHERE game_id = ? LIMIT 1").get(mapping.game_id));
  if (!recapture) {
    const active = database.prepare(
      "SELECT snapshot_id FROM active_game_source_snapshots WHERE game_id = ?",
    ).get(mapping.game_id);
    if (active) return associateReuse(Number(active.snapshot_id));
    const priorCapture = database.prepare(
      "SELECT snapshot_id FROM source_game_snapshots WHERE game_id = ? AND provider = 'fiba_livestats' ORDER BY snapshot_id DESC LIMIT 1",
    ).get(mapping.game_id);
    if (priorCapture) return associateReuse(Number(priorCapture.snapshot_id));
    if (oldImport) {
      throw new Error(`SOURCE_SNAPSHOT_REQUIRED: game ${mapping.game_id} has prior imported data but no original capture; use --recapture-source to explicitly capture a new source version`);
    }
  }

  const captureKind = recapture
    ? oldImport && !hasPriorSnapshot ? "historical_recapture" : "explicit_recapture"
    : "initial_import";
  const runId = randomUUID();
  database.prepare("INSERT INTO source_import_runs(import_run_id, provider, capture_kind, status) VALUES (?, 'fiba_livestats', ?, 'capturing')").run(runId, captureKind);
  return fetchSourceBody(mapping.json_url).then(({ body, data, parseError, contentType, contentEncoding }) => {
    const snapshotId = captureSourceSnapshot(database, mapping, body, { contentType, contentEncoding }, captureKind);
    database.prepare("INSERT OR IGNORE INTO source_import_run_snapshots(import_run_id, snapshot_id) VALUES (?, ?)").run(runId, snapshotId);
    if (parseError) throw new Error(`SOURCE_JSON_PARSE_ERROR: ${parseError.message}`);
    database.prepare("UPDATE source_import_runs SET status = 'captured' WHERE import_run_id = ?").run(runId);
    return { snapshotId, runId, importRunId: runId, data, newlyCaptured: true };
  }).catch((error) => {
    database.prepare("UPDATE source_import_runs SET status = 'failed', completed_at = CURRENT_TIMESTAMP, error_message = ? WHERE import_run_id = ?").run(error.message, runId);
    throw error;
  });
}

function persistSnapshotInterpretation(database, mapping, data, snapshotId, playerResolver, options = {}) {
  const deferIdentityDecisions = options.deferIdentityDecisions === true;
  const normalizerVersion = "arc-livestats-source-v3";
  const resolverVersion = "identity-resolver-phase1-v2";
  const shotObjectsByActionNumber = indexLiveStatsShotObjects(data);
  const normalizedJson = stableJson(data);
  const normalizedHash = sha256(Buffer.from(normalizedJson, "utf8"));
  let normalization = database.prepare(
    "SELECT normalization_id FROM source_snapshot_normalizations WHERE snapshot_id = ? AND normalizer_version = ?",
  ).get(snapshotId, normalizerVersion);
  if (!normalization) {
    const result = database.prepare(`
      INSERT INTO source_snapshot_normalizations
        (snapshot_id, game_id, normalizer_version, normalized_json_gzip, normalized_sha256, status)
      VALUES (?, ?, ?, ?, ?, 'complete')
    `).run(snapshotId, mapping.game_id, normalizerVersion, gzipSync(Buffer.from(normalizedJson, "utf8")), normalizedHash);
    normalization = { normalization_id: Number(result.lastInsertRowid) };
  } else if (database.prepare("SELECT normalized_sha256 FROM source_snapshot_normalizations WHERE normalization_id = ?").get(normalization.normalization_id).normalized_sha256 !== normalizedHash) {
    throw new Error(`NORMALIZED_SNAPSHOT_HASH_MISMATCH: normalization ${normalization.normalization_id}`);
  }
  const normalizationId = Number(normalization.normalization_id);
  const existingOccurrences = database.prepare("SELECT COUNT(*) AS count FROM source_team_occurrences WHERE normalization_id = ?").get(normalizationId).count;
  if (existingOccurrences > 0) {
    const rosterOccurrenceIds = new Map(database.prepare(`SELECT identity_occurrence_key,player_occurrence_id FROM source_player_occurrences
      WHERE normalization_id = ?`).all(normalizationId).map((row) => [row.identity_occurrence_key, Number(row.player_occurrence_id)]));
    const rosterRows = [];
    for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
      for (const [entryKey, player] of Object.entries(team.pl ?? {})) {
        const identity = playerResolver.rosterOccurrence(teamNumber, player);
        const occurrenceId = rosterOccurrenceIds.get(identity.key);
        if (!occurrenceId) throw new Error(`SOURCE_OCCURRENCE_MISMATCH: ${identity.key}`);
        rosterRows.push({ occurrenceId, occurrenceKey: identity.key, playerId: identity.identity?.player_id ?? null,
          confidence: identity.identity?.confidence ?? null,
          status: identity.identity?.status ?? "unresolved", evidence: { teamNumber, entryKey,
            providerPlayerId: player.playerId ?? null, jerseyNumber: player.shirtNumber ?? null,
          pno: player.pno ?? null, name: player.name ?? player.scoreboardName ?? null,
          resolverEvidence: identity.identity?.evidence ?? null } });
      }
    }
    const resolverVersion = "identity-resolver-phase1-v2";
    const rosterCanonical = rosterRows.map(({ occurrenceKey, playerId, status, confidence, evidence }) => ({ occurrenceKey, playerId, status, confidence, evidence }));
    const decisionHash = sha256(Buffer.from(stableJson({ resolverVersion, decisions: rosterCanonical }), "utf8"));
    const setStatus = rosterRows.some((row) => !row.playerId) ? "contains_review" : "complete";
    const identityDecisionRows = rosterRows.map((row) => ({
      player_occurrence_id: row.occurrenceId,
      identity_occurrence_key: row.occurrenceKey,
      arc_player_id: row.playerId,
      status: row.playerId ? "resolved" : ["ambiguous", "conflict", "unresolved", "review"].includes(row.status) ? row.status : "review",
      decision_source: "resolver",
      evidence_json: stableJson({ ...row.evidence, resolverOutcome: row.status, confidence: row.confidence }),
    }));
    if (deferIdentityDecisions) {
      const eventOccurrenceIds = database.prepare(`SELECT event_occurrence_id FROM source_event_occurrences
        WHERE normalization_id = ? ORDER BY source_ordinal`).all(normalizationId).map((row) => Number(row.event_occurrence_id));
      return { snapshotId, normalizationId, decisionSetId: null, rosterOccurrenceIds,
        decisionIds: new Map(), eventOccurrenceIds, identityDecisionRows, shotObjectsByActionNumber,
        decisionSetSha256: decisionHash, resolverVersion, decisionSetStatus: setStatus };
    }
    let decision = database.prepare(`SELECT decision_set_id,decision_set_sha256,status FROM identity_decision_sets
      WHERE normalization_id = ? ORDER BY decision_set_version DESC LIMIT 1`).get(normalizationId);
    if (!decision || decision.decision_set_sha256 !== decisionHash || decision.status !== "complete") {
      const version = database.prepare("SELECT COALESCE(MAX(decision_set_version),0)+1 AS version FROM identity_decision_sets WHERE normalization_id = ?").get(normalizationId).version;
      const result = database.prepare(`INSERT INTO identity_decision_sets
        (normalization_id,decision_set_version,resolver_version,decision_set_sha256,status)
        VALUES(?,?,?,?, ?)`).run(normalizationId, version, resolverVersion, decisionHash, setStatus);
      decision = { decision_set_id: Number(result.lastInsertRowid), status: setStatus, decision_set_sha256: decisionHash };
      const insertDecision = database.prepare(`INSERT INTO identity_decisions
        (decision_set_id,normalization_id,player_occurrence_id,arc_player_id,status,decision_source,evidence_json)
        VALUES(?,?,?,?,?,'resolver',?)`);
      for (const row of rosterRows) {
        const status = row.playerId ? "resolved" : ["ambiguous", "conflict", "unresolved", "review"].includes(row.status) ? row.status : "review";
        insertDecision.run(decision.decision_set_id, normalizationId, row.occurrenceId, row.playerId, status,
          stableJson({ ...row.evidence, resolverOutcome: row.status, confidence: row.confidence }));
      }
    }
    const decisionIds = new Map(database.prepare(`SELECT po.identity_occurrence_key,id.identity_decision_id
      FROM identity_decisions id JOIN source_player_occurrences po ON po.player_occurrence_id=id.player_occurrence_id
      WHERE id.decision_set_id = ?`).all(decision.decision_set_id).map((row) => [row.identity_occurrence_key, Number(row.identity_decision_id)]));
    const eventOccurrenceIds = database.prepare(`SELECT event_occurrence_id FROM source_event_occurrences
      WHERE normalization_id = ? ORDER BY source_ordinal`).all(normalizationId).map((row) => Number(row.event_occurrence_id));
    return { snapshotId, normalizationId, decisionSetId: Number(decision.decision_set_id), rosterOccurrenceIds, decisionIds, eventOccurrenceIds, shotObjectsByActionNumber };
  }

  const teamIds = new Map();
  const rosterIds = new Map();
  const rosterRows = [];
  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    const teamResult = database.prepare(`INSERT INTO source_team_occurrences
      (normalization_id,source_ordinal,source_team_key,provider_team_id,team_name,home_away)
      VALUES(?,?,?,?,?,?)`).run(
      normalizationId, teamIds.size, teamNumber, providerTeamId(team), team.name,
      teamNumber === "1" ? "home" : teamNumber === "2" ? "away" : null,
    );
    const teamOccurrenceId = Number(teamResult.lastInsertRowid);
    teamIds.set(String(teamNumber), teamOccurrenceId);
    for (const [rosterOrdinal, [entryKey, player]] of Object.entries(Object.entries(team.pl ?? {}))) {
      const result = playerResolver.rosterOccurrence(teamNumber, player);
      const playerResult = database.prepare(`INSERT INTO source_player_occurrences
        (normalization_id,team_occurrence_id,identity_occurrence_key,source_ordinal,roster_entry_key,provider_player_id,pno,original_name,display_name,first_name,last_name,jersey_number,position,starter)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
        normalizationId, teamOccurrenceId, result.key, Number(rosterOrdinal), entryKey,
        normalizeText(player.playerId), normalizeText(player.pno), normalizeText(player.name),
        normalizeText(player.scoreboardName), normalizeText(player.internationalFirstName ?? player.firstName),
        normalizeText(player.internationalFamilyName ?? player.familyName), normalizeText(player.shirtNumber),
        normalizeText(player.playingPosition), toInteger(player.starter),
      );
      const playerOccurrenceId = Number(playerResult.lastInsertRowid);
      rosterIds.set(result.key, playerOccurrenceId);
      rosterRows.push({ occurrenceId: playerOccurrenceId, occurrenceKey: result.key, playerId: result.identity?.player_id ?? null, confidence: result.identity?.confidence ?? null, status: result.identity?.status ?? "unresolved", evidence: { teamNumber, entryKey, providerPlayerId: player.playerId ?? null, jerseyNumber: player.shirtNumber ?? null, pno: player.pno ?? null, name: player.name ?? player.scoreboardName ?? null, resolverEvidence: result.identity?.evidence ?? null } });
    }
  }

  const rosterCanonical = rosterRows.map(({ occurrenceKey, playerId, status, confidence, evidence }) => ({ occurrenceKey, playerId, status, confidence, evidence }));
  const decisionHash = sha256(Buffer.from(stableJson({ resolverVersion, decisions: rosterCanonical }), "utf8"));
  const decisionSetStatus = rosterRows.some((row) => !row.playerId) ? "contains_review" : "complete";
  const identityDecisionRows = rosterRows.map((row) => ({
    player_occurrence_id: row.occurrenceId,
    identity_occurrence_key: row.occurrenceKey,
    arc_player_id: row.playerId,
    status: row.playerId ? "resolved" : ["ambiguous", "conflict", "unresolved", "review"].includes(row.status) ? row.status : "review",
    decision_source: "resolver",
    evidence_json: stableJson({ ...row.evidence, resolverOutcome: row.status, confidence: row.confidence }),
  }));
  let decisionSetId = null;
  const decisionIds = new Map();
  if (!deferIdentityDecisions) {
    const versionRow = database.prepare("SELECT COALESCE(MAX(decision_set_version),0)+1 AS version FROM identity_decision_sets WHERE normalization_id = ?").get(normalizationId);
    const decisionSetResult = database.prepare(`INSERT INTO identity_decision_sets
      (normalization_id,decision_set_version,resolver_version,decision_set_sha256,status)
      VALUES(?,?,?,?, ?)`).run(normalizationId, versionRow.version, resolverVersion, decisionHash, decisionSetStatus);
    decisionSetId = Number(decisionSetResult.lastInsertRowid);
    const insertDecision = database.prepare(`INSERT INTO identity_decisions
      (decision_set_id,normalization_id,player_occurrence_id,arc_player_id,status,decision_source,evidence_json)
      VALUES(?,?,?,?,?,? ,?)`);
    for (const row of identityDecisionRows) {
      const inserted = insertDecision.run(decisionSetId, normalizationId, row.player_occurrence_id,
        row.arc_player_id, row.status, row.decision_source, row.evidence_json);
      decisionIds.set(row.identity_occurrence_key, Number(inserted.lastInsertRowid));
    }
  }

  const eventOccurrenceIds = [];
  const insertEventOccurrence = database.prepare(`INSERT INTO source_event_occurrences
    (normalization_id,source_ordinal,source_event_id,provider_action_number,period,clock,team_occurrence_id,action_type,subtype,success,primary_player_occurrence_id,secondary_player_occurrence_id,primary_original_name,primary_provider_player_id,primary_pno,primary_jersey_number,secondary_original_name,secondary_provider_player_id,secondary_pno,secondary_jersey_number,primary_identity_status,secondary_identity_status,previous_action_number,provider_shot_previous_action,provider_shot_result,provider_shot_x,provider_shot_y,provider_shot_player_ref)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  for (const [ordinal, action] of parseJsonArray(data.pbp).entries()) {
    const primaryKey = playerResolver.eventOccurrenceKey(action, "primary");
    const secondaryKey = playerResolver.eventOccurrenceKey(action, "secondary");
    const shotObject = shotObjectsByActionNumber.get(toInteger(action.actionNumber));
    const inserted = insertEventOccurrence.run(
      normalizationId, ordinal, normalizeText(action.eventId ?? action.actionId), toInteger(action.actionNumber),
      toInteger(action.period), normalizeText(action.clock), teamIds.get(String(action.tno)) ?? null,
      normalizeText(action.actionType), normalizeText(action.subType), toInteger(action.success),
      rosterIds.get(primaryKey) ?? null, rosterIds.get(secondaryKey) ?? null,
      normalizeText(action.player), normalizeText(action.playerId), normalizeText(action.pno), normalizeText(action.shirtNumber),
      normalizeText(action.secondaryPlayer ?? action.otherPlayer ?? action.relatedPlayer),
      normalizeText(action.secondaryPlayerId ?? action.playerId2 ?? action.relatedPlayerId),
      normalizeText(action.secondaryPno ?? action.pno2 ?? action.relatedPno),
      normalizeText(action.secondaryShirtNumber ?? action.shirtNumber2 ?? action.relatedShirtNumber),
      playerResolver.eventIdentityStatus(action, "primary"),
      playerResolver.eventIdentityStatus(action, "secondary"),
      normalizeText(action.previousAction), normalizeText(shotObject?.previousAction), shotObject?.result ?? null, shotObject?.x ?? null,
      shotObject?.y ?? null, normalizeText(shotObject?.p),
    );
    eventOccurrenceIds.push(Number(inserted.lastInsertRowid));
  }
  return { snapshotId, normalizationId, decisionSetId, rosterOccurrenceIds: rosterIds, decisionIds,
    eventOccurrenceIds, identityDecisionRows, shotObjectsByActionNumber, decisionSetSha256: decisionHash,
    resolverVersion, decisionSetStatus };
}

function upsertTeam(database, teamName, provider, externalTeamId = null) {
  const canonicalName = normalizeText(teamName);
  if (!canonicalName) {
    throw new Error("Team name is required for ARC team resolution");
  }

  let teamId = null;

  if (externalTeamId) {
    const existingByExternalId = database
      .prepare(
        `
        SELECT team_id
        FROM team_external_ids
        WHERE provider = ? AND external_team_id = ?
        `,
      )
      .get(provider, String(externalTeamId));
    if (existingByExternalId) {
      teamId = Number(existingByExternalId.team_id);
    }
  }

  if (teamId === null) {
    const existingByName = database
      .prepare(
        `
        SELECT t.team_id, t.canonical_name
        FROM teams t
        LEFT JOIN team_aliases ta ON ta.team_id = t.team_id
        WHERE t.canonical_name = ? COLLATE NOCASE
           OR ta.alias_name = ? COLLATE NOCASE
        ORDER BY t.team_id
        LIMIT 1
        `,
      )
      .get(canonicalName, canonicalName);
    if (existingByName) {
      teamId = Number(existingByName.team_id);
    }
  }

  if (teamId === null) {
    const insertResult = database
      .prepare(
        `
        INSERT INTO teams (canonical_name)
        VALUES (?)
        `,
      )
      .run(canonicalName);
    teamId = Number(insertResult.lastInsertRowid);
  }

  if (externalTeamId) {
    database
      .prepare(
        `
        INSERT OR IGNORE INTO team_external_ids (team_id, provider, external_team_id)
        VALUES (?, ?, ?)
        `,
      )
      .run(teamId, provider, String(externalTeamId));
  }

  const existing = database.prepare("SELECT canonical_name FROM teams WHERE team_id = ?").get(teamId);
  if (existing && normalizeText(existing.canonical_name)?.toLowerCase() !== canonicalName.toLowerCase()) {
    database
      .prepare(
        `
        INSERT OR IGNORE INTO team_aliases (team_id, alias_name, source)
        VALUES (?, ?, ?)
        `,
      )
      .run(teamId, canonicalName, provider);
  }

  return teamId;
}

function importTeamGames(database, gameId, homeTeamId, awayTeamId, homeTeam, awayTeam) {
  const metricFields = {
    period_1_points: "p1_score", period_2_points: "p2_score", period_3_points: "p3_score", period_4_points: "p4_score",
    overtime_points: "ot_score", field_goals_made: "tot_sFieldGoalsMade", field_goals_attempted: "tot_sFieldGoalsAttempted",
    three_pointers_made: "tot_sThreePointersMade", three_pointers_attempted: "tot_sThreePointersAttempted",
    free_throws_made: "tot_sFreeThrowsMade", free_throws_attempted: "tot_sFreeThrowsAttempted",
    off_reb: "tot_sReboundsOffensive", def_reb: "tot_sReboundsDefensive", tot_reb: "tot_sReboundsTotal",
    team_rebounds: "tot_sReboundsTeam", team_off_reb: "tot_sReboundsTeamOffensive", team_def_reb: "tot_sReboundsTeamDefensive",
    assists: "tot_sAssists", steals: "tot_sSteals", blocks: "tot_sBlocks", blocks_received: "tot_sBlocksReceived",
    turnovers: "tot_sTurnovers", team_turnovers: "tot_sTurnoversTeam", personal_fouls: "tot_sFoulsPersonal",
    fouls_drawn: "tot_sFoulsOn", team_fouls: "tot_sFoulsTeam", total_fouls: "tot_sFoulsTotal",
    points_fast_break: "tot_sPointsFastBreak", points_from_turnovers: "tot_sPointsFromTurnovers",
    points_in_paint: "tot_sPointsInThePaint", points_second_chance: "tot_sPointsSecondChance", bench_points: "tot_sBenchPoints",
    biggest_lead: "tot_sBiggestLead", biggest_scoring_run: "tot_sBiggestScoringRun", lead_changes: "tot_sLeadChanges",
    times_scores_level: "tot_sTimesScoresLevel",
  };
  const metricColumns = Object.keys(metricFields);
  const columns = ["game_id", "team_id", "opponent_team_id", "is_home", "points", ...metricColumns, "provider_team_id"];
  const updateColumns = columns.filter((column) => !["game_id", "team_id"].includes(column));
  const insert = database.prepare(
    `INSERT INTO team_games (${columns.map((column) => `"${column}"`).join(",")})
     VALUES (${columns.map(() => "?").join(",")})
     ON CONFLICT (game_id, team_id) DO UPDATE SET
     ${updateColumns.map((column) => `"${column}"=excluded."${column}"`).join(",")}`,
  );
  const values = (teamId, opponentTeamId, isHome, team) => [
    gameId, teamId, opponentTeamId, isHome, toInteger(team.score),
    ...metricColumns.map((column) => toInteger(team[metricFields[column]])), providerTeamId(team),
  ];
  insert.run(...values(homeTeamId, awayTeamId, 1, homeTeam));
  insert.run(...values(awayTeamId, homeTeamId, 0, awayTeam));
}

function importPlayerGames(database, gameId, data, teamIdsByLiveStatsNumber, playerResolver, sourceContext) {
  if (!playerGamesColumnsCache) {
    const columns = database.prepare("PRAGMA table_info(player_games)").all();
    playerGamesColumnsCache = new Set(columns.map((column) => column.name));
  }

  const hasProviderPlayerId = playerGamesColumnsCache.has("provider_player_id");
  const playerColumns = [
    "game_id", "player_id", "team_id", ...(hasProviderPlayerId ? ["provider_player_id"] : []),
    "player_name", "team_name", "shirt_number", "position", "minutes", "points",
    "points_fast_break", "points_in_paint", "points_second_chance", "off_reb", "def_reb", "tot_reb",
    "assists", "steals", "blocks", "blocks_received", "personal_fouls", "fouls_drawn", "turnovers",
    "fgm", "fga", "tpm", "tpa", "ftm", "fta", "plus_minus", "starter", "captain",
  ];
  const immutableColumns = new Set(["game_id", "player_name", "team_name", "shirt_number", "player_id"]);
  const updateColumns = playerColumns.filter((column) => !immutableColumns.has(column));
  const insert = database.prepare(
    `INSERT INTO player_games (${playerColumns.map((column) => `"${column}"`).join(",")})
     VALUES (${playerColumns.map(() => "?").join(",")})
     ON CONFLICT (game_id, player_name, team_name, shirt_number) DO UPDATE SET
     ${updateColumns.map((column) => `"${column}"=excluded."${column}"`).join(",")}`,
  );

  let imported = 0;

  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    const teamId = teamIdsByLiveStatsNumber.get(teamNumber);
    const teamName = team.name;

    for (const player of Object.values(team.pl ?? {})) {
      const playerId = playerResolver.playerIdForRoster(teamNumber, player);

      const commonValues = [
        gameId,
        playerId,
        teamId,
        ...(hasProviderPlayerId ? [player.playerId ?? null] : []),
        player.name,
        teamName,
        String(player.shirtNumber ?? ""),
        player.playingPosition ?? null,
        player.sMinutes ?? null,
        toInteger(player.sPoints),
        toInteger(player.sPointsFastBreak),
        toInteger(player.sPointsInThePaint),
        toInteger(player.sPointsSecondChance),
        toInteger(player.sReboundsOffensive),
        toInteger(player.sReboundsDefensive),
        toInteger(player.sReboundsTotal),
        toInteger(player.sAssists),
        toInteger(player.sSteals),
        toInteger(player.sBlocks),
        toInteger(player.sBlocksReceived),
        toInteger(player.sFoulsPersonal),
        toInteger(player.sFoulsOn),
        toInteger(player.sTurnovers),
        toInteger(player.sFieldGoalsMade),
        toInteger(player.sFieldGoalsAttempted),
        toInteger(player.sThreePointersMade),
        toInteger(player.sThreePointersAttempted),
        toInteger(player.sFreeThrowsMade),
        toInteger(player.sFreeThrowsAttempted),
        toInteger(player.sPlusMinusPoints),
        toInteger(player.starter),
        toInteger(player.captain),
      ];
      insert.run(...commonValues);

      const occurrence = playerResolver.rosterOccurrence(teamNumber, player);
      const occurrenceId = sourceContext.rosterOccurrenceIds.get(occurrence.key);
      const decisionId = sourceContext.decisionIds.get(occurrence.key);
      database.prepare(`UPDATE player_games SET source_player_occurrence_id = ?, identity_decision_id = ?
        WHERE game_id = ? AND player_name = ? AND team_name = ? AND shirt_number = ?`)
        .run(occurrenceId ?? null, decisionId ?? null, gameId, player.name, teamName, String(player.shirtNumber ?? ""));

      imported += 1;
    }
  }

  return imported;
}

function updateGame(database, mapping, homeTeamId, awayTeamId, homeTeam, awayTeam) {
  database
    .prepare(
      `
      UPDATE games
      SET
        home_team_id = ?,
        away_team_id = ?,
        home_score = ?,
        away_score = ?,
        status = ?
      WHERE game_id = ?
      `,
    )
    .run(
      homeTeamId,
      awayTeamId,
      toInteger(homeTeam.score),
      toInteger(awayTeam.score),
      "boxscore_imported",
      mapping.game_id,
    );
}

function selectGamesForEventsImport(database) {
  return database
    .prepare(
      `
      SELECT s.*, g.home_team_id, g.away_team_id
      FROM source_livestats_games s
      JOIN games g ON g.game_id = s.game_id
      WHERE g.home_team_id IS NOT NULL
        AND g.away_team_id IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM events e WHERE e.game_id = s.game_id
        )
      ORDER BY s.provider_game_id
      `,
    )
    .all();
}

async function importMapping(database, mapping, options = {}) {
  const captured = await acquireSourceSnapshot(database, mapping, options.recaptureSource === true);
  const data = captured.data;
  const homeTeam = data.tm?.["1"];
  const awayTeam = data.tm?.["2"];

  if (!homeTeam || !awayTeam) {
    throw new Error(`Missing home/away team data for match_id ${mapping.match_id}`);
  }

  database.exec("BEGIN;");
  let transactionOpen = true;
  let committedReview = false;
  try {
    const homeTeamId = upsertTeam(database, homeTeam.name, "fiba_livestats", providerTeamId(homeTeam));
    const awayTeamId = upsertTeam(database, awayTeam.name, "fiba_livestats", providerTeamId(awayTeam));

    const teamIdsByLiveStatsNumber = new Map([
      ["1", homeTeamId],
      ["2", awayTeamId],
    ]);
    const playerResolver = resolveLiveStatsPlayerOccurrences(
      mapping,
      data,
      teamIdsByLiveStatsNumber,
      database,
    );
    const sourceContext = persistSnapshotInterpretation(database, mapping, data, captured.snapshotId, playerResolver,
      { deferIdentityDecisions: playerResolver.reviews.length === 0 });
    if (playerResolver.reviews.length) {
      const candidateRunId = captured.importRunId ?? captured.runId;
      database.prepare(`INSERT INTO source_import_analytical_candidates
        (import_run_id,game_id,snapshot_id,normalization_id,decision_set_id,status,candidate_json)
        VALUES(?,?,?,?,?,'review_required',?)`).run(candidateRunId, mapping.game_id, sourceContext.snapshotId,
        sourceContext.normalizationId, sourceContext.decisionSetId,
        stableJson({ reviews: playerResolver.reviews, identity_decisions: sourceContext.identityDecisionRows ?? [] }));
      database.exec("COMMIT;");
      transactionOpen = false;
      committedReview = true;
      if (captured.importRunId) database.prepare("UPDATE source_import_runs SET status='failed', completed_at=CURRENT_TIMESTAMP, error_message=? WHERE import_run_id=?").run("Identity resolution requires manual review", captured.importRunId);
      const error = new Error("Identity resolution requires manual review");
      error.identityReviews = playerResolver.reviews;
      throw error;
    }
    const candidateRunId = captured.importRunId ?? captured.runId;
    if (!candidateRunId) throw new Error(`SOURCE_IMPORT_RUN_REQUIRED: ${mapping.game_id}`);
    let candidateProjection;
    database.exec("SAVEPOINT arc_candidate_projection;");
    try {
      applyIdentityChanges(database, playerResolver.identityChanges);
      updateGame(database, mapping, homeTeamId, awayTeamId, homeTeam, awayTeam);
      importTeamGames(database, mapping.game_id, homeTeamId, awayTeamId, homeTeam, awayTeam);
      for (const [teamNumber] of Object.entries(data.tm ?? {})) {
        const occurrence = database.prepare(`SELECT team_occurrence_id FROM source_team_occurrences
          WHERE normalization_id = ? AND source_team_key = ? ORDER BY source_ordinal LIMIT 1`)
          .get(sourceContext.normalizationId, String(teamNumber));
        if (occurrence) database.prepare("UPDATE team_games SET source_team_occurrence_id=? WHERE game_id=? AND team_id=?")
          .run(occurrence.team_occurrence_id, mapping.game_id, teamIdsByLiveStatsNumber.get(String(teamNumber)));
      }
      const playerGames = importPlayerGames(database, mapping.game_id, data, teamIdsByLiveStatsNumber, playerResolver, sourceContext);
      // Candidate projection replaces events inside this savepoint. Remove their
      // dependent links first; rollback restores the active relationships.
      database.prepare("DELETE FROM event_relationships WHERE game_id=?").run(mapping.game_id);
      const events = importEvents(database, mapping.game_id, data, teamIdsByLiveStatsNumber, playerResolver, sourceContext);
      const asObjects = (rows) => rows.map((row) => Object.fromEntries(Object.entries(row)));
      candidateProjection = {
        game: Object.fromEntries(Object.entries(database.prepare("SELECT * FROM games WHERE game_id=?").get(mapping.game_id))),
        team_games: asObjects(database.prepare("SELECT * FROM team_games WHERE game_id=? ORDER BY team_id").all(mapping.game_id)),
        player_games: asObjects(database.prepare("SELECT * FROM player_games WHERE game_id=? ORDER BY player_game_id").all(mapping.game_id)),
        events: asObjects(database.prepare("SELECT * FROM events WHERE game_id=? ORDER BY action_number,event_id").all(mapping.game_id)),
        playerGamesImported: playerGames,
        eventsImported: events.imported,
        eventTypesHandled: events.eventTypesHandled,
      };
      database.exec("ROLLBACK TO arc_candidate_projection;");
      database.exec("RELEASE arc_candidate_projection;");
    } catch (error) {
      database.exec("ROLLBACK TO arc_candidate_projection;");
      database.exec("RELEASE arc_candidate_projection;");
      throw error;
    }
    const bundle = {
      identity_changes: playerResolver.identityChanges,
      identity_base_sha256: identityStateSha256(database),
      resolver_version: sourceContext.resolverVersion,
      decision_set_sha256: sourceContext.decisionSetSha256,
      decision_set_status: sourceContext.decisionSetStatus,
      identity_decisions: sourceContext.identityDecisionRows,
      projection: candidateProjection,
    };
    database.prepare(`INSERT INTO source_import_analytical_candidates
      (import_run_id,game_id,snapshot_id,normalization_id,status,candidate_json)
      VALUES(?,?,?,?, 'ready_for_reconstruction',?)`).run(candidateRunId, mapping.game_id,
      sourceContext.snapshotId, sourceContext.normalizationId, stableJson(bundle));
    database.exec("COMMIT;");
    transactionOpen = false;

    return {
      gameId: mapping.game_id,
      matchId: mapping.match_id,
      homeTeam: homeTeam.name,
      awayTeam: awayTeam.name,
      playerGames: candidateProjection.playerGamesImported,
      events: candidateProjection.eventsImported,
      eventTypesHandled: candidateProjection.eventTypesHandled,
      snapshotId: sourceContext.snapshotId,
      importRunId: candidateRunId,
    };
  } catch (error) {
    if (transactionOpen) database.exec("ROLLBACK;");
    if (!committedReview && captured.importRunId) database.prepare("UPDATE source_import_runs SET status='failed', completed_at=CURRENT_TIMESTAMP, error_message=? WHERE import_run_id=?").run(error.message, captured.importRunId);
    throw error;
  }
}

function promoteCandidate(importRunId) {
  const args = [lineupReconstructorPath, "--database", databasePath, "--promote-import-run-id", importRunId];
  const process = spawnSync(
    identityPython,
    args,
    { encoding: "utf8", windowsHide: true },
  );

  if (process.error) {
    throw new Error(`Lineup reconstructor could not start (${identityPython}): ${process.error.message}`);
  }

  const output = {};
  for (const line of (process.stdout ?? "").split(/\r?\n/)) {
    const separatorIndex = line.indexOf("=");
    if (separatorIndex === -1) continue;
    output[line.slice(0, separatorIndex)] = line.slice(separatorIndex + 1);
  }

  if (process.status !== 0) {
    throw new Error(
      output.error || (process.stderr ?? "").trim() || `Candidate promotion failed for import ${importRunId}`,
    );
  }

  return {
    status: output.status ?? "unknown",
    lineupsCreated: Number(output.lineups_created ?? 0),
    lineupSegmentsCreated: Number(output.lineup_segments_created ?? 0),
  };
}

(async function main() {
  const args = parseArgs(process.argv);
  const database = initializeDatabase(databasePath);
  ensurePhase2Columns(database);

  // Initialize DLQ for tracking failed imports
  const dlq = new DeadLetterQueue(database);

  try {
    if (args.retryCandidate) {
      const result = promoteCandidate(args.retryCandidate);
      console.log(`Promoted candidate ${args.retryCandidate}: status=${result.status}, lineup_segments=${result.lineupSegmentsCreated}`);
      return;
    }
    // ── Events-only bulk population mode ──────────────────────────────────────
    if (args.eventsOnly) {
      const gameRows = selectGamesForEventsImport(database);
      if (gameRows.length === 0) {
        console.log("No games require event import.");
        process.exit(0);
      }
      let gamesSucceeded = 0;
      let gamesFailed = 0;
      let totalEventsInserted = 0;
      for (const gameRow of gameRows) {
        try {
          const hasActiveSource = database.prepare("SELECT 1 FROM active_game_source_snapshots WHERE game_id=?").get(gameRow.game_id);
          const candidate = await importMapping(database, gameRow, {
            recaptureSource: args.recaptureSource || !hasActiveSource,
          });
          const promotion = promoteCandidate(candidate.importRunId);
          gamesSucceeded += 1;
          totalEventsInserted += candidate.events;
          console.log(`Promoted event-complete candidate ${candidate.gameId}: ${promotion.lineupSegmentsCreated} lineup segments`);
        } catch (gameError) {
          gamesFailed += 1;
          console.error(`failed ${gameRow.game_id} (${gameRow.match_id}): ${gameError.message}`);
        }
      }

      console.log("");
      console.log(`games processed: ${gameRows.length}`);
      console.log(`games succeeded: ${gamesSucceeded}`);
      console.log(`games failed: ${gamesFailed}`);
      console.log(`events inserted: ${totalEventsInserted}`);
      process.exit(gamesFailed > 0 ? 1 : 0);
    }
    // ── End events-only mode ─────────────────────────────────────────────────

    const mappings = selectMappings(database, args);

    if (mappings.length === 0) {
      console.log("No LiveStats games to import.");
      process.exit(0);
    }

    let totalPlayerGames = 0;
    let totalEvents = 0;
    let maxEventTypesHandled = 0;
    let successCount = 0;
    let failureCount = 0;
    let totalLineups = 0;
    let totalLineupSegments = 0;
    let lineupFailureCount = 0;

    for (const mapping of mappings) {
      try {
        const result = await importMapping(database, mapping, args);
        totalPlayerGames += result.playerGames;
        totalEvents += result.events;
        maxEventTypesHandled = Math.max(maxEventTypesHandled, result.eventTypesHandled);
        successCount += 1;
        console.log(
          `Imported ${result.gameId} (${result.matchId}): ${result.homeTeam} vs ${result.awayTeam}, player_games=${result.playerGames}, events=${result.events}, event_types=${result.eventTypesHandled}`,
        );

        // Lineup reconstruction runs only after a successful boxscore import,
        // and its outcome must never flip an already-succeeded game to failed.
        try {
          const lineupResult = promoteCandidate(result.importRunId);
          totalLineups += lineupResult.lineupsCreated;
          totalLineupSegments += lineupResult.lineupSegmentsCreated;
          console.log(
            `Lineups for ${result.gameId}: status=${lineupResult.status}, lineups=${lineupResult.lineupsCreated}, lineup_segments=${lineupResult.lineupSegmentsCreated}`,
          );
        } catch (lineupError) {
          lineupFailureCount += 1;
          console.error(`Lineup reconstruction failed for ${result.gameId}: ${lineupError.message}`);
          const identityError = [
            "AMBIGUOUS_PLAYER_IDENTITY",
            "PLAYER_IDENTITY_CONFLICT",
            "UNRESOLVED_PLAYER_IDENTITY",
          ].find((code) => lineupError.message.includes(code));

          try {
            dlq.recordFailure(mapping, identityError ? identityError.toLowerCase() : "lineup_reconstruction_error", lineupError.message, {
              provider: "fiba_livestats",
              entityType: identityError ? "player_occurrence" : "game",
              entityKey: result.gameId,
              context: {
                game_id: mapping.game_id,
                league_id: mapping.league_id,
                season_id: mapping.season_id,
                match_id: mapping.match_id,
                provider_game_id: mapping.provider_game_id,
                identity_error_type: identityError ?? null,
              },
            });
          } catch (dlqError) {
            console.error(
              `DLQ record failed for lineup reconstruction of ${result.gameId}: ${dlqError.message}`,
            );
          }
        }
      } catch (error) {
        failureCount += 1;

        // Determine error type for DLQ categorization
        let errorType = "unknown_error";
        const identityError = [
          "AMBIGUOUS_PLAYER_IDENTITY",
          "PLAYER_IDENTITY_CONFLICT",
          "UNRESOLVED_PLAYER_IDENTITY",
        ].find((code) => error.message.includes(code));
        if (error.identityReviews?.length) {
          errorType = "identity_review";
        } else if (identityError) {
          errorType = identityError.toLowerCase();
        } else if (error.message.includes("LiveStats request failed")) {
          errorType = "network_error";
        } else if (error.message.includes("Unexpected end of JSON input")) {
          errorType = "json_parse_error";
        } else if (error.message.includes("Missing")) {
          errorType = "data_validation_error";
        }

        // Record failure in DLQ for later analysis/retry.
        // DLQ issues must not abort processing of remaining games.
        try {
          dlq.recordFailure(mapping, errorType, error.message, {
            provider: "fiba_livestats",
            entityType: error.identityReviews?.length || identityError ? "player_occurrence" : null,
            entityKey: error.identityReviews?.map((review) => review.key).join(",") || null,
            context: {
              game_id: mapping.game_id,
              league_id: mapping.league_id,
              season_id: mapping.season_id,
              match_id: mapping.match_id,
              provider_game_id: mapping.provider_game_id,
              identity_reviews: error.identityReviews ?? [],
              identity_error_type: identityError ?? null,
            },
          });
        } catch (dlqError) {
          console.error(
            `DLQ record failed for ${mapping.game_id} (${mapping.match_id}): ${dlqError.message}`,
          );
        }

        // Mark game as failed in games table
        database
          .prepare(
            `
            UPDATE games
            SET status = ?
            WHERE game_id = ?
            `,
          )
          .run("boxscore_import_failed", mapping.game_id);

        console.error(
          `Failed ${mapping.game_id} (${mapping.match_id}): ${error.message}`,
        );
      }
    }

    console.log("");
    console.log(`Import complete: succeeded=${successCount}, failed=${failureCount}`);
    console.log(`Imported games: ${successCount}`);
    console.log(`Imported player_games: ${totalPlayerGames}`);
    console.log(`Imported events: ${totalEvents}`);
    console.log(`Event types handled (max per game): ${maxEventTypesHandled}`);
    console.log(`Lineups created: ${totalLineups}`);
    console.log(`Lineup segments created: ${totalLineupSegments}`);
    if (lineupFailureCount > 0) {
      console.log(`Lineup reconstruction failures: ${lineupFailureCount}`);
    }

    // Report DLQ stats
    const dlqStats = dlq.getFailureStats();
    if (dlqStats.length > 0) {
      console.log("\nDead Letter Queue Status:");
      for (const stat of dlqStats) {
        console.log(`  ${stat.status}: ${stat.count} (max_retries=${stat.max_retries})`);
      }
    }
  } finally {
    database.close();
  }
})();
