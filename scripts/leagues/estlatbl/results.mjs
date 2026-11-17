const RESULTS_TABLE = /<table\b(?=[^>]*\bclass\s*=\s*(["'])[^"']*\btable\b[^"']*\bstandings\b[^"']*\bscheduleAndResults\b[^"']*\1)[^>]*>([\s\S]*?)<\/table\s*>/i;
const ROW = /<tr\b[^>]*>([\s\S]*?)<\/tr\s*>/gi;
const CELL = /<td\b([^>]*)>([\s\S]*?)<\/td\s*>/gi;

function attribute(attributes, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = attributes.match(new RegExp(`\\b${escaped}\\s*=\\s*(["'])([\\s\\S]*?)\\1`, "i"));
  return match?.[2] ?? null;
}

function classes(attributes) {
  return new Set((attribute(attributes, "class") ?? "").split(/\s+/).filter(Boolean));
}

function decodeHtml(value) {
  return value
    .replace(/&nbsp;|&#160;|&#xA0;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([\da-f]+);/gi, (_, code) => String.fromCodePoint(parseInt(code, 16)));
}

function textContent(fragment) {
  return decodeHtml(fragment.replace(/<!--[\s\S]*?-->/g, "").replace(/<[^>]*>/g, " "))
    .replace(/\s+/g, " ")
    .trim();
}

function innerByClass(fragment, tagName, className) {
  const tag = tagName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const expression = new RegExp(`<${tag}\\b([^>]*)>([\\s\\S]*?)<\\/${tag}\\s*>`, "gi");
  for (const match of fragment.matchAll(expression)) {
    if (classes(match[1]).has(className)) return textContent(match[2]);
  }
  return null;
}

function gameIdFromClick(attributes) {
  const click = attribute(attributes, "onclick") ?? "";
  const match = click.match(/doRedirectGame\(\s*(['"])https:\/\/www\.estlatbl\.com\/et\/tulemused\/(\d+)\/#c\1\s*\)/i);
  return match?.[2] ?? null;
}

function parseDisplayedDate(value) {
  const match = value.match(/^(?:[\p{L}]{1,3}\s+)?(\d{1,2})\.(\d{1,2})\.(\d{4}),\s*(\d{1,2}):(\d{2})$/u);
  if (!match) return null;
  const [, dayText, monthText, yearText, hourText, minuteText] = match;
  const day = Number(dayText);
  const month = Number(monthText);
  const year = Number(yearText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  if (hour > 23 || minute > 59) return null;
  return { gameDate: `${yearText}-${monthText.padStart(2, "0")}-${dayText.padStart(2, "0")}`, displayedTime: `${hourText.padStart(2, "0")}:${minuteText}` };
}

function parseRow(rowHtml) {
  const cells = [];
  for (const match of rowHtml.matchAll(CELL)) {
    cells.push({ attributes: match[1], body: match[2], classNames: classes(match[1]) });
  }
  const dateCell = cells.find((cell) => cell.classNames.has("dateAndTimeTd"));
  if (!dateCell) return null;

  const providerGameId = gameIdFromClick(dateCell.attributes);
  const dateText = innerByClass(dateCell.body, "div", "dateAndTime");
  const parsedDate = dateText ? parseDisplayedDate(dateText) : null;
  const venue = innerByClass(dateCell.body, "div", "arena");
  const notPlayed = /\bmängu\s+ei\s+toimu\b/i.test(venue ?? "");
  const homeCell = cells.find((cell) => cell.classNames.has("homeTeam"));
  const resultCell = cells.find((cell) => cell.classNames.has("result"));
  const awayCell = cells.find((cell) => cell.classNames.has("awayTeam"));
  const homeTeam = homeCell ? innerByClass(homeCell.body, "span", "homeTeamNameDesktop") : null;
  const awayTeam = awayCell ? innerByClass(awayCell.body, "span", "visitorTeamNameDesktop") : null;
  const scoreText = resultCell ? textContent(resultCell.body) : "";
  const score = scoreText.match(/^(\d+)\s*:\s*(\d+)$/);
  const scheduledWithoutScore = scoreText === "" || scoreText === "-";
  const reasons = [];
  if (!providerGameId) reasons.push("missing_or_unrecognized_game_link");
  if (!parsedDate && !notPlayed) reasons.push("missing_or_invalid_displayed_date");
  if (!homeTeam) reasons.push("missing_home_team");
  if (!awayTeam) reasons.push("missing_away_team");
  if (!resultCell) reasons.push("missing_result_cell");
  if (!score && !scheduledWithoutScore) reasons.push("unrecognized_result_value");
  const linkedIds = new Set(cells.map((cell) => gameIdFromClick(cell.attributes)).filter(Boolean));
  if (linkedIds.size > 1) reasons.push("conflicting_game_links_within_row");

  return {
    providerGameId,
    gameDate: parsedDate?.gameDate ?? null,
    displayedTime: parsedDate?.displayedTime ?? null,
    homeTeam,
    awayTeam,
    homeScore: score ? Number(score[1]) : null,
    awayScore: score ? Number(score[2]) : null,
    venue: venue || null,
    ...(notPlayed ? { notPlayed: true } : {}),
    completed: !notPlayed && Boolean(score) && reasons.length === 0,
    issues: reasons,
  };
}

export function parseEstlatblResultsPage(html) {
  if (typeof html !== "string") throw new TypeError("EstLatBL results HTML must be a string");
  const tableMatch = html.match(RESULTS_TABLE);
  if (!tableMatch) throw new Error("Official EstLatBL results table .table.standings.scheduleAndResults was not found");

  const parsedRecords = [];
  const issues = [];
  let discovered = 0;
  for (const match of tableMatch[2].matchAll(ROW)) {
    const record = parseRow(match[1]);
    if (!record) continue;
    discovered += 1;
    parsedRecords.push(record);
    if (record.issues.length) {
      issues.push({ providerGameId: record.providerGameId, reasons: record.issues });
    }
  }

  const counts = new Map();
  for (const record of parsedRecords) {
    if (record.providerGameId) counts.set(record.providerGameId, (counts.get(record.providerGameId) ?? 0) + 1);
  }
  const duplicateIds = new Set([...counts].filter(([, count]) => count > 1).map(([id]) => id));
  for (const providerGameId of duplicateIds) {
    issues.push({ providerGameId, reasons: ["duplicate_provider_game_id_in_results_page"] });
  }
  const flaggedRows = parsedRecords.filter((record) => record.issues.length > 0 || duplicateIds.has(record.providerGameId)).length;

  return {
    records: parsedRecords.filter((record) => !record.notPlayed && record.issues.length === 0 && !duplicateIds.has(record.providerGameId)),
    notPlayedRecords: parsedRecords.filter((record) => record.notPlayed && record.issues.length === 0 && !duplicateIds.has(record.providerGameId)),
    issues,
    discovered,
    flaggedRows,
  };
}
