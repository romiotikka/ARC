import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { DatabaseSync } from "node:sqlite";
import { DeadLetterQueue } from "../lib/estlatbl-utils.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = join(__dirname, "..", "..");
const databasePath = process.env.ARC_DATABASE_PATH ?? join(rootDir, "data", "arc2.db");
const identityBridgePath = join(rootDir, "scripts", "identity", "resolve_livestats_players.py");
const identityPython = process.env.ARC_PYTHON ?? process.env.PYTHON ?? "python";

function parseArgs(argv) {
  const args = {
    limit: 1,
    matchId: null,
    eventsOnly: false,
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
    }
  }

  return args;
}

function slugify(value) {
  return value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
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
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/gi, "")
    .toLowerCase()
    .trim();
}

let playerGamesColumnsCache = null;

function occurrenceKey(teamNumber, playerName, externalPlayerId = null) {
  return `${teamNumber}|${normalizePlayerKey(playerName)}|${externalPlayerId ?? ""}`;
}

function resolveLiveStatsPlayerOccurrences(mapping, data, teamIdsByLiveStatsNumber) {
  const playerLookupByTeam = buildLiveStatsPlayerLookup(data);
  const occurrences = new Map();
  const rosterKeys = new Map();

  const addOccurrence = (teamNumber, playerName, details = {}) => {
    const rawName = normalizeText(playerName);
    const teamId = teamIdsByLiveStatsNumber.get(String(teamNumber));
    if (!rawName || !teamId) return null;

    const key = occurrenceKey(teamNumber, rawName, details.externalPlayerId);
    if (!occurrences.has(key)) {
      occurrences.set(key, {
        key,
        raw_name: rawName,
        team_id: teamId,
        season_id: mapping.season_id,
        league_id: mapping.league_id,
        game_id: mapping.game_id,
        provider: "fiba_livestats",
        jersey_number: normalizeText(details.jerseyNumber),
        position: normalizeText(details.position),
        external_player_id: normalizeText(details.externalPlayerId),
      });
    }
    return key;
  };

  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    for (const player of Object.values(team.pl ?? {})) {
      const key = addOccurrence(teamNumber, player.name ?? player.scoreboardName, {
        externalPlayerId: player.playerId,
        jerseyNumber: player.shirtNumber,
        position: player.playingPosition,
      });
      if (key) {
        rosterKeys.set(`${teamNumber}|${normalizePlayerKey(player.name ?? player.scoreboardName)}`, key);
      }
    }
  }

  const eventKeys = new Map();
  for (const action of parseJsonArray(data.pbp)) {
    const teamNumber = String(action.tno ?? "");
    for (const playerName of [
      resolvePlayerNameFromAction(action, playerLookupByTeam),
      resolveSecondaryPlayerName(action, playerLookupByTeam),
    ]) {
      const rawName = normalizeText(playerName);
      if (!rawName) continue;
      const rosterKey = rosterKeys.get(`${teamNumber}|${normalizePlayerKey(rawName)}`);
      const key = rosterKey ?? addOccurrence(teamNumber, rawName);
      if (key) eventKeys.set(`${teamNumber}|${normalizePlayerKey(rawName)}`, key);
    }
  }

  const process = spawnSync(identityPython, [identityBridgePath, "--database", databasePath], {
    input: JSON.stringify({ occurrences: [...occurrences.values()] }),
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
  const playerIdForKey = (key) => {
    const playerId = resolved[key]?.player_id;
    if (!playerId) throw new Error(`IdentityResolver returned no player_id for ${key}`);
    return playerId;
  };

  return {
    playerIdForRoster(teamNumber, player) {
      const name = player.name ?? player.scoreboardName;
      return playerIdForKey(occurrenceKey(teamNumber, name, player.playerId));
    },
    playerIdForEvent(teamNumber, playerName) {
      const key = eventKeys.get(`${teamNumber}|${normalizePlayerKey(playerName)}`);
      return key ? playerIdForKey(key) : null;
    },
  };
}

function buildLiveStatsPlayerLookup(data) {
  const lookup = new Map();

  for (const [teamNumber, team] of Object.entries(data.tm ?? {})) {
    const teamLookup = new Map();

    for (const [playerKey, player] of Object.entries(team.pl ?? {})) {
      const name = normalizeText(player.name);
      if (!name) {
        continue;
      }

      const keyVariants = [
        normalizeText(playerKey),
        normalizeText(player.playerId),
        normalizeText(player.pno),
        normalizeText(player.shirtNumber),
      ].filter(Boolean);

      for (const variant of keyVariants) {
        teamLookup.set(variant, name);
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
  if (!teamLookup) {
    return null;
  }

  const pnoKey = normalizeText(action.pno);
  if (pnoKey && teamLookup.has(pnoKey)) {
    return teamLookup.get(pnoKey);
  }

  const shirtNumberKey = normalizeText(action.shirtNumber);
  if (shirtNumberKey && teamLookup.has(shirtNumberKey)) {
    return teamLookup.get(shirtNumberKey);
  }

  return null;
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

  const playerNumberFields = [
    "secondaryPno",
    "secondary_pno",
    "pno2",
    "relatedPno",
    "assistPno",
    "stealPno",
    "blockPno",
    "drawnByPno",
    "fouledPno",
    "defenderPno",
  ];

  for (const field of playerNumberFields) {
    const value = normalizeText(action[field]);
    if (value && teamLookup.has(value)) {
      return teamLookup.get(value);
    }
  }

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

  const distance = Math.sqrt(x * x + y * y);
  return { x, y, distance };
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

function eventMetadata(action) {
  const mappedKeys = new Set([
    "actionNumber",
    "period",
    "clock",
    "tno",
    "player",
    "pno",
    "actionType",
    "success",
    "subType",
    "scoring",
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
  for (const [key, value] of Object.entries(action ?? {})) {
    if (mappedKeys.has(key)) {
      continue;
    }

    metadata[key] = value;
  }

  return Object.keys(metadata).length > 0 ? JSON.stringify(metadata) : null;
}

function importEvents(database, gameId, data, teamIdsByLiveStatsNumber, playerResolver) {
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
      distance,
      possession_team_id_after,
      shot_clock,
      contested,
      metadata_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
      distance = excluded.distance,
      possession_team_id_after = excluded.possession_team_id_after,
      shot_clock = excluded.shot_clock,
      contested = excluded.contested,
      metadata_json = excluded.metadata_json
    `,
  );

  const normalizedEvents = pbp.map((action, index) => {
    const actionType = normalizeText(action.actionType)?.toLowerCase() ?? "unknown";
    const subType = normalizeText(action.subType)?.toLowerCase() ?? null;
    const success = toInteger(action.success);
    const teamId = teamIdsByLiveStatsNumber.get(String(action.tno)) ?? null;
    const opponentTeamId = teamId
      ? [...teamIdsByLiveStatsNumber.values()].find((value) => value !== teamId) ?? null
      : null;

    const playerName = resolvePlayerNameFromAction(action, playerLookupByTeam);
    const secondaryName = resolveSecondaryPlayerName(action, playerLookupByTeam);

    const playerId = playerName
      ? playerResolver.playerIdForEvent(String(action.tno ?? ""), playerName)
      : null;
    const secondaryPlayerId =
      secondaryName && secondaryName !== playerName
        ? playerResolver.playerIdForEvent(String(action.tno ?? ""), secondaryName)
        : null;

    const pointsFromScoring = toInteger(action.scoring);
    const points =
      pointsFromScoring ??
      (success === 1 && actionType === "2pt"
        ? 2
        : success === 1 && actionType === "3pt"
          ? 3
          : success === 1 && actionType === "freethrow"
            ? 1
            : null);

    const shotType = ["2pt", "3pt", "freethrow"].includes(actionType) ? actionType : null;
    const shotMove = shotType ? subType : null;
    const { x, y, distance } = extractCoordinates(action);

    return {
      index,
      gameId,
      actionNumber: toInteger(action.actionNumber) ?? index + 1,
      period: toInteger(action.period),
      clock: normalizeClock(action.clock),
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
      distance,
      shotClock: null,
      contested: null,
      metadataJson: eventMetadata(action),
      possessionTeamIdAfter: null,
      subType,
      success,
    };
  });

  const byChronologicalOrder = [...normalizedEvents].sort((left, right) => {
    if (left.actionNumber !== right.actionNumber) {
      return left.actionNumber - right.actionNumber;
    }
    return left.index - right.index;
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
      event.distance,
      event.possessionTeamIdAfter,
      event.shotClock,
      event.contested,
      event.metadataJson,
    );
  }

  return {
    imported: normalizedEvents.length,
    eventTypesHandled: eventTypes.size,
  };
}

function teamIdFromName(teamName) {
  return `team_${slugify(teamName)}`;
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

function upsertTeam(database, teamName) {
  const teamId = teamIdFromName(teamName);

  database
    .prepare(
      `
      INSERT INTO teams (team_id, canonical_name)
      VALUES (?, ?)
      ON CONFLICT (team_id) DO UPDATE SET
        canonical_name = excluded.canonical_name
      `,
    )
    .run(teamId, teamName);

  database
    .prepare(
      `
      INSERT INTO team_aliases (team_id, alias_name, source)
      VALUES (?, ?, ?)
      ON CONFLICT (team_id, alias_name) DO NOTHING
      `,
    )
    .run(teamId, teamName, "fiba_livestats");

  return teamId;
}

function importTeamGames(database, gameId, homeTeamId, awayTeamId, homeTeam, awayTeam) {
  const insert = database.prepare(
    `
    INSERT INTO team_games (
      game_id,
      team_id,
      opponent_team_id,
      is_home,
      points
    )
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT (game_id, team_id) DO UPDATE SET
      opponent_team_id = excluded.opponent_team_id,
      is_home = excluded.is_home,
      points = excluded.points
    `,
  );

  insert.run(gameId, homeTeamId, awayTeamId, 1, toInteger(homeTeam.score));
  insert.run(gameId, awayTeamId, homeTeamId, 0, toInteger(awayTeam.score));
}

function importPlayerGames(database, gameId, data, teamIdsByLiveStatsNumber, playerResolver) {
  if (!playerGamesColumnsCache) {
    const columns = database.prepare("PRAGMA table_info(player_games)").all();
    playerGamesColumnsCache = new Set(columns.map((column) => column.name));
  }

  const hasProviderPlayerId = playerGamesColumnsCache.has("provider_player_id");

  const insert = hasProviderPlayerId
    ? database.prepare(
        `
        INSERT INTO player_games (
          game_id,
          player_id,
          team_id,
          provider_player_id,
          player_name,
          team_name,
          shirt_number,
          position,
          minutes,
          points,
          off_reb,
          def_reb,
          tot_reb,
          assists,
          steals,
          blocks,
          turnovers,
          fgm,
          fga,
          tpm,
          tpa,
          ftm,
          fta,
          plus_minus,
          starter
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_id, player_name, team_name, shirt_number) DO UPDATE SET
          team_id = excluded.team_id,
          provider_player_id = excluded.provider_player_id,
          position = excluded.position,
          minutes = excluded.minutes,
          points = excluded.points,
          off_reb = excluded.off_reb,
          def_reb = excluded.def_reb,
          tot_reb = excluded.tot_reb,
          assists = excluded.assists,
          steals = excluded.steals,
          blocks = excluded.blocks,
          turnovers = excluded.turnovers,
          fgm = excluded.fgm,
          fga = excluded.fga,
          tpm = excluded.tpm,
          tpa = excluded.tpa,
          ftm = excluded.ftm,
          fta = excluded.fta,
          plus_minus = excluded.plus_minus,
          starter = excluded.starter
        `,
      )
    : database.prepare(
        `
        INSERT INTO player_games (
          game_id,
          player_id,
          team_id,
          player_name,
          team_name,
          shirt_number,
          position,
          minutes,
          points,
          off_reb,
          def_reb,
          tot_reb,
          assists,
          steals,
          blocks,
          turnovers,
          fgm,
          fga,
          tpm,
          tpa,
          ftm,
          fta,
          plus_minus,
          starter
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (game_id, player_name, team_name, shirt_number) DO UPDATE SET
          team_id = excluded.team_id,
          position = excluded.position,
          minutes = excluded.minutes,
          points = excluded.points,
          off_reb = excluded.off_reb,
          def_reb = excluded.def_reb,
          tot_reb = excluded.tot_reb,
          assists = excluded.assists,
          steals = excluded.steals,
          blocks = excluded.blocks,
          turnovers = excluded.turnovers,
          fgm = excluded.fgm,
          fga = excluded.fga,
          tpm = excluded.tpm,
          tpa = excluded.tpa,
          ftm = excluded.ftm,
          fta = excluded.fta,
          plus_minus = excluded.plus_minus,
          starter = excluded.starter
        `,
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
        player.name,
        teamName,
        String(player.shirtNumber ?? ""),
        player.playingPosition ?? null,
        player.sMinutes ?? null,
        toInteger(player.sPoints),
        toInteger(player.sReboundsOffensive),
        toInteger(player.sReboundsDefensive),
        toInteger(player.sReboundsTotal),
        toInteger(player.sAssists),
        toInteger(player.sSteals),
        toInteger(player.sBlocks),
        toInteger(player.sTurnovers),
        toInteger(player.sFieldGoalsMade),
        toInteger(player.sFieldGoalsAttempted),
        toInteger(player.sThreePointersMade),
        toInteger(player.sThreePointersAttempted),
        toInteger(player.sFreeThrowsMade),
        toInteger(player.sFreeThrowsAttempted),
        toInteger(player.sPlusMinusPoints),
        toInteger(player.starter),
      ];

      if (hasProviderPlayerId) {
        insert.run(
          gameId,
          playerId,
          teamId,
          player.playerId ?? null,
          ...commonValues.slice(3),
        );
      } else {
        insert.run(...commonValues);
      }

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

async function prepareEventsForGame(gameRow) {
  const data = await fetchJson(gameRow.json_url);
  const homeTeam = data.tm?.["1"];
  const awayTeam = data.tm?.["2"];

  if (!homeTeam || !awayTeam) {
    throw new Error(`Missing home/away team data for match_id ${gameRow.match_id}`);
  }

  const teamIdsByLiveStatsNumber = new Map([
    ["1", gameRow.home_team_id],
    ["2", gameRow.away_team_id],
  ]);

  const playerResolver = resolveLiveStatsPlayerOccurrences(gameRow, data, teamIdsByLiveStatsNumber);
  return { data, teamIdsByLiveStatsNumber, playerResolver };
}

async function importEventsForGame(database, gameRow, prepared = null) {
  const source = prepared ?? await prepareEventsForGame(gameRow);
  const result = importEvents(
    database,
    gameRow.game_id,
    source.data,
    source.teamIdsByLiveStatsNumber,
    source.playerResolver,
  );
  return result.imported;
}

async function importMapping(database, mapping) {
  const data = await fetchJson(mapping.json_url);
  const homeTeam = data.tm?.["1"];
  const awayTeam = data.tm?.["2"];

  if (!homeTeam || !awayTeam) {
    throw new Error(`Missing home/away team data for match_id ${mapping.match_id}`);
  }

  const homeTeamId = teamIdFromName(homeTeam.name);
  const awayTeamId = teamIdFromName(awayTeam.name);

  const teamIdsByLiveStatsNumber = new Map([
    ["1", homeTeamId],
    ["2", awayTeamId],
  ]);

  const playerResolver = resolveLiveStatsPlayerOccurrences(mapping, data, teamIdsByLiveStatsNumber);

  database.exec("BEGIN;");
  try {
    upsertTeam(database, homeTeam.name);
    upsertTeam(database, awayTeam.name);
    updateGame(database, mapping, homeTeamId, awayTeamId, homeTeam, awayTeam);
    importTeamGames(database, mapping.game_id, homeTeamId, awayTeamId, homeTeam, awayTeam);
    const playerGames = importPlayerGames(
      database,
      mapping.game_id,
      data,
      teamIdsByLiveStatsNumber,
      playerResolver,
    );
    const events = importEvents(
      database,
      mapping.game_id,
      data,
      teamIdsByLiveStatsNumber,
      playerResolver,
    );
    database.exec("COMMIT;");

    return {
      gameId: mapping.game_id,
      matchId: mapping.match_id,
      homeTeam: homeTeam.name,
      awayTeam: awayTeam.name,
      playerGames,
      events: events.imported,
      eventTypesHandled: events.eventTypesHandled,
    };
  } catch (error) {
    database.exec("ROLLBACK;");
    throw error;
  }
}

(async function main() {
  const args = parseArgs(process.argv);
  const database = new DatabaseSync(databasePath);
  database.exec("PRAGMA foreign_keys = ON;");

  // Initialize DLQ for tracking failed imports
  const dlq = new DeadLetterQueue(database);

  try {
    // ── Events-only bulk population mode ──────────────────────────────────────
    if (args.eventsOnly) {
      const gameRows = selectGamesForEventsImport(database);

      if (gameRows.length === 0) {
        console.log("No games require event import.");
        process.exit(0);
      }

      // Step 1: 1-game validation
      const validationGame = gameRows[0];
      let validationPassed = false;
      let validationEvents = 0;

      let validationTransactionStarted = false;
      try {
        const validationPrepared = await prepareEventsForGame(validationGame);
        database.exec("BEGIN;");
        validationTransactionStarted = true;
        validationEvents = await importEventsForGame(database, validationGame, validationPrepared);
        const newViolations = database
          .prepare(
            `SELECT COUNT(*) AS cnt FROM pragma_foreign_key_check WHERE \"table\" = 'events'`,
          )
          .get().cnt;
        if (validationEvents > 0 && newViolations === 0) {
          validationPassed = true;
          database.exec("COMMIT;");
          validationTransactionStarted = false;
        } else {
          database.exec("ROLLBACK;");
          validationTransactionStarted = false;
        }
      } catch (validationError) {
        if (validationTransactionStarted) database.exec("ROLLBACK;");
        console.error(`validation error: ${validationError.message}`);
      }

      console.log(`game_id: ${validationGame.game_id}`);
      console.log(`events inserted: ${validationEvents}`);
      console.log(`validation: ${validationPassed ? "passed" : "failed"}`);

      if (!validationPassed) {
        process.exit(1);
      }

      // Step 2: Bulk import remaining games (skip the already-done validation game)
      let gamesProcessed = 1;
      let gamesSucceeded = 1;
      let gamesFailed = 0;
      let totalEventsInserted = validationEvents;
      let runtimeErrorCount = 0;

      for (const gameRow of gameRows.slice(1)) {
        gamesProcessed += 1;
        let transactionStarted = false;
        try {
          const prepared = await prepareEventsForGame(gameRow);
          database.exec("BEGIN;");
          transactionStarted = true;
          const inserted = await importEventsForGame(database, gameRow, prepared);
          database.exec("COMMIT;");
          transactionStarted = false;
          gamesSucceeded += 1;
          totalEventsInserted += inserted;
        } catch (gameError) {
          if (transactionStarted) database.exec("ROLLBACK;");
          gamesFailed += 1;
          runtimeErrorCount += 1;
          console.error(`failed ${gameRow.game_id} (${gameRow.match_id}): ${gameError.message}`);
        }
      }

      console.log("");
      console.log(`games processed: ${gamesProcessed}`);
      console.log(`games succeeded: ${gamesSucceeded}`);
      console.log(`games failed: ${gamesFailed}`);
      console.log(`events inserted: ${totalEventsInserted}`);
      console.log(`runtime errors count: ${runtimeErrorCount}`);
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

    for (const mapping of mappings) {
      try {
        const result = await importMapping(database, mapping);
        totalPlayerGames += result.playerGames;
        totalEvents += result.events;
        maxEventTypesHandled = Math.max(maxEventTypesHandled, result.eventTypesHandled);
        successCount += 1;
        console.log(
          `Imported ${result.gameId} (${result.matchId}): ${result.homeTeam} vs ${result.awayTeam}, player_games=${result.playerGames}, events=${result.events}, event_types=${result.eventTypesHandled}`,
        );
      } catch (error) {
        failureCount += 1;

        // Determine error type for DLQ categorization
        let errorType = "unknown_error";
        if (error.message.includes("LiveStats request failed")) {
          errorType = "network_error";
        } else if (error.message.includes("Unexpected end of JSON input")) {
          errorType = "json_parse_error";
        } else if (error.message.includes("Missing")) {
          errorType = "data_validation_error";
        }

        // Record failure in DLQ for later analysis/retry.
        // DLQ issues must not abort processing of remaining games.
        try {
          dlq.recordFailure(mapping, errorType, error.message);
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
