export function validateEstlatblMapping(database, season, gameId, providerGameId, matchId) {
  const rows = database.prepare(`
    SELECT s.game_id, s.season_id, s.provider_game_id, s.match_id
    FROM source_livestats_games s
    WHERE s.provider_game_id = ? OR s.match_id = ?
  `).all(String(providerGameId), String(matchId));
  const issues = [];
  if (rows.length > 1) issues.push("duplicate_provider_or_match_mapping");
  for (const row of rows) {
    if (row.game_id !== gameId) issues.push("provider_mapping_points_to_other_game");
    if (Number(row.season_id) !== Number(season.seasonId)) issues.push("mapping_season_conflict");
    if (String(row.provider_game_id) !== String(providerGameId)) issues.push("provider_game_id_conflict");
    if (String(row.match_id) !== String(matchId)) issues.push("fiba_match_id_conflict");
  }
  return [...new Set(issues)];
}

export function findExistingEstlatblMapping(database, gameId, providerGameId, season) {
  const rows = database.prepare(`
    SELECT game_id, season_id, provider_game_id, match_id
    FROM source_livestats_games
    WHERE provider_game_id = ? OR game_id = ?
  `).all(String(providerGameId), gameId);
  if (rows.length > 1) return { status: "conflict", issues: ["duplicate_provider_game_mapping"] };
  if (rows.length === 0) return { status: "missing", issues: [] };

  const row = rows[0];
  const issues = [];
  if (row.game_id !== gameId) issues.push("provider_mapping_points_to_other_game");
  if (String(row.provider_game_id) !== String(providerGameId)) issues.push("provider_game_id_conflict");
  if (Number(row.season_id) !== Number(season.seasonId)) issues.push("mapping_season_conflict");
  if (row.match_id === null || row.match_id === undefined || String(row.match_id).trim() === "") {
    issues.push("missing_fiba_match_id");
  }
  if (issues.length) return { status: "conflict", issues };
  return { status: "matched", matchId: String(row.match_id) };
}

function normalizeTeamName(value) {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("en")
    .replace(/[^a-z0-9]/g, "");
}

function teamNameMatches(database, teamId, officialName) {
  const names = database.prepare(`
    SELECT canonical_name AS name FROM teams WHERE team_id = ?
    UNION SELECT alias_name AS name FROM team_aliases WHERE team_id = ?
  `).all(teamId, teamId).map((row) => normalizeTeamName(row.name));
  return names.includes(normalizeTeamName(officialName));
}

export function validateEstlatblResultMetadata(database, gameId, season, result) {
  const row = database.prepare(`
    SELECT g.season_id, g.home_team_id, g.away_team_id, g.home_score, g.away_score
    FROM games g WHERE g.game_id = ?
  `).get(gameId);
  if (!row) return [];
  const issues = [];
  if (Number(row.season_id) !== Number(season.seasonId)) issues.push("game_season_conflict");
  if (row.home_team_id !== null && !teamNameMatches(database, row.home_team_id, result.homeTeam)) issues.push("home_team_conflict");
  if (row.away_team_id !== null && !teamNameMatches(database, row.away_team_id, result.awayTeam)) issues.push("away_team_conflict");
  if (row.home_score !== null && Number(row.home_score) !== result.homeScore) issues.push("home_score_conflict");
  if (row.away_score !== null && Number(row.away_score) !== result.awayScore) issues.push("away_score_conflict");
  return issues;
}

// League scores are independent result evidence. A score disagreement must be
// reported, but must not prevent safe date collection or invalidate a usable
// FIBA source import. Team and season disagreements remain hard conflicts.
export function classifyEstlatblResultMetadataIssues(issues, { allowKnownScoreDiscrepancy = false } = {}) {
  const scoreDiscrepancies = allowKnownScoreDiscrepancy
    ? issues.filter((issue) => issue === "home_score_conflict" || issue === "away_score_conflict")
    : [];
  const conflicts = allowKnownScoreDiscrepancy
    ? issues.filter((issue) => issue !== "home_score_conflict" && issue !== "away_score_conflict")
    : [...issues];
  return { conflicts, scoreDiscrepancies };
}
