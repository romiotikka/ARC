function validatedDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  return value;
}

export function persistValidatedGameDate(database, gameId, dateValue) {
  const gameDate = validatedDate(dateValue);
  if (!gameDate) return { status: "invalid_date" };
  if (typeof gameId !== "string" || gameId.trim() === "") return { status: "invalid_game_id" };

  const game = database.prepare("SELECT game_date FROM games WHERE game_id = ?").get(gameId);
  if (!game) return { status: "game_not_found" };
  if (game.game_date === gameDate) return { status: "unchanged" };
  if (game.game_date !== null && game.game_date !== undefined && String(game.game_date).trim() !== "") {
    return { status: "conflict", existingDate: game.game_date, candidateDate: gameDate };
  }

  database.prepare("UPDATE games SET game_date = ? WHERE game_id = ? AND (game_date IS NULL OR trim(game_date) = '')")
    .run(gameDate, gameId);
  return { status: "updated" };
}
