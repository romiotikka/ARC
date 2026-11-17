#!/usr/bin/env node

import { DatabaseSync } from "node:sqlite";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve, basename } from "node:path";
import { fileURLToPath } from "node:url";

const COLUMNS = [
  "provider_game_id",
  "season_id",
  "game_date",
  "home_team",
  "away_team",
  "home_score",
  "away_score",
  "match_id",
  "source_url",
  "notes",
  "verification_status",
];

const DEFAULT_MANIFEST =
  ".tmp-2021-22-mapping-audit-20261115/mapping-audit.json";

const DEFAULT_CSV =
  "data/estlatbl_2021_22_mapping_overrides.csv";

const esc = value =>
  `"${String(value ?? "").replaceAll('"', '""')}"`;


function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;

  for (let i = 0; i < text.length; i++) {
    const c = text[i];

    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') {
        quoted = false;
      } else {
        cell += c;
      }
    } else if (c === '"') {
      quoted = true;
    } else if (c === ",") {
      row.push(cell);
      cell = "";
    } else if (c === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (c !== "\r") {
      cell += c;
    }
  }

  if (quoted) {
    throw Error("Malformed CSV quoting");
  }

  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }

  const [head, ...body] = rows;

  if (JSON.stringify(head) !== JSON.stringify(COLUMNS)) {
    throw Error(`CSV header must be ${COLUMNS.join(",")}`);
  }

  return body
    .filter(r => r.some(Boolean))
    .map(r =>
      Object.fromEntries(
        COLUMNS.map((key, i) => [key, r[i] ?? ""])
      )
    );
}


// --------------------------------------------------
// TEAM NAME NORMALIZATION
// --------------------------------------------------

const norm = s =>
  String(s ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");


const TEAM_ALIASES = new Map([
  ["taltechoptibet", "taltech"],
  ["kkviimsisportland", "kkviimsi"],
]);


const canonicalTeam = name => {
  const normalized = norm(name);
  return TEAM_ALIASES.get(normalized) ?? normalized;
};


function resolveTeamId(db, name) {
  const variants = new Set([
    canonicalTeam(name),
    canonicalTeam(
      String(name).replace(/^(?:KK|BC)\s+/i, "")
    ),
  ]);

  const rows = db.prepare(`
    SELECT team_id, canonical_name AS name
    FROM teams

    UNION ALL

    SELECT team_id, alias_name AS name
    FROM team_aliases
  `).all();

  const ids = [
    ...new Set(
      rows
        .filter(r => variants.has(canonicalTeam(r.name)))
        .map(r => Number(r.team_id))
    ),
  ];

  if (ids.length !== 1) {
    throw Error(
      `team '${name}' resolves to ${ids.length} ARC IDs`
    );
  }

  return ids[0];
}


// --------------------------------------------------
// HTTP / HTML HELPERS
// --------------------------------------------------

const htmlText = s =>
  s
    .replace(/<script\b[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();


async function fetchOk(url) {
  const response = await fetch(url, {
    signal: AbortSignal.timeout(20000),
  });

  if (!response.ok) {
    throw Error(`HTTP ${response.status}`);
  }

  return response;
}


// --------------------------------------------------
// MAPPING VALIDATION
// --------------------------------------------------

export async function validateMapping(row, fetcher = fetchOk) {
  const out = {
    ...row,
    verification_status: "UNVERIFIED",
    validation_notes: "",
  };

  if (!String(row.match_id ?? "").trim()) {
    out.validation_notes = "match_id is blank";
    return out;
  }

  if (
    String(row.season_id) !== "20212022" ||
    !/^202[12]-\d\d-\d\d$/.test(row.game_date)
  ) {
    out.verification_status = "CONFLICT";
    out.validation_notes =
      "season/date does not describe a 2021–22 game";
    return out;
  }

  const expected = [
    {
      name: canonicalTeam(row.home_team),
      score: Number(row.home_score),
    },
    {
      name: canonicalTeam(row.away_team),
      score: Number(row.away_score),
    },
  ];

  if (
    expected.some(
      team =>
        !team.name ||
        !Number.isFinite(team.score)
    )
  ) {
    out.verification_status = "UNVERIFIED";
    out.validation_notes =
      "incomplete expected team/score metadata";
    return out;
  }

  try {
    const id = encodeURIComponent(row.match_id.trim());

    const jsonUrl =
      `https://fibalivestats.dcd.shared.geniussports.com/data/${id}/data.json`;

    const pageUrl =
      `https://fibalivestats.dcd.shared.geniussports.com/u/EBF/${id}/p.html`;

    const [jsonResponse, pageResponse] = await Promise.all([
      fetcher(jsonUrl),
      fetcher(pageUrl),
    ]);

    let json;

    try {
      json = await jsonResponse.json();
    } catch {
      out.verification_status = "CONFLICT";
      out.validation_notes = "endpoint response is not JSON";
      return out;
    }

    const pair = Object.values(json.tm ?? {}).slice(0, 2);

    if (
      pair.length !== 2 ||
      pair.some(
        team =>
          !team.name ||
          !Number.isFinite(Number(team.score))
      )
    ) {
      out.verification_status = "UNVERIFIED";
      out.validation_notes =
        "incomplete team/score metadata";
      return out;
    }

    const actual = pair.map(team => ({
      name: canonicalTeam(team.name),
      score: Number(team.score),
    }));

    // Both teams and their scores must match.
    // The FIBA team order may be reversed.
    const matches = (a, b) =>
      a.every(
        (team, i) =>
          team.name === b[i].name &&
          team.score === b[i].score
      );

    const teamsAndScoresMatch =
      matches(actual, expected) ||
      matches(actual, [...expected].reverse());

    const teamsMatch =
      matches(
        actual.map(team => ({
          name: team.name,
          score: 0,
        })),
        expected.map(team => ({
          name: team.name,
          score: 0,
        }))
      ) ||
      matches(
        actual.map(team => ({
          name: team.name,
          score: 0,
        })),
        [...expected].reverse().map(team => ({
          name: team.name,
          score: 0,
        }))
      );

    const page = htmlText(await pageResponse.text());

    const competitionMatch =
      /Paf Estonian-Latvian Basketball League|Estonian-Latvian Basketball League/i
        .test(page);

    const date = page.match(
      /Tip off:\s*\d{1,2}:\d{2}\s*(?:am|pm)?\s*(?:GMT)?\s*(\d{1,2})\/(\d{1,2})\/(\d{2,4})/i
    );

    let dateMatch = false;

    if (date) {
      let year = Number(date[3]);

      if (year < 100) {
        year += 2000;
      }

      const fibaDate =
        `${year}-${String(Number(date[2])).padStart(2, "0")}-${String(Number(date[1])).padStart(2, "0")}`;

      dateMatch = fibaDate === row.game_date;
    }

    const pageTeamsMatch =
      page.includes(`${pair[0].name} vs ${pair[1].name}`) ||
      page.includes(`${pair[1].name} vs ${pair[0].name}`);

    out.verification_status =
      teamsAndScoresMatch &&
      competitionMatch &&
      dateMatch &&
      pageTeamsMatch
        ? "VERIFIED"
        : "CONFLICT";

    out.validation_notes =
      `teams=${teamsMatch}; ` +
      `teams_and_scores=${teamsAndScoresMatch}; ` +
      `date=${date ? dateMatch : "unavailable"}; ` +
      `competition=${competitionMatch}; ` +
      `page_teams=${pageTeamsMatch}; ` +
      `FIBA=${pair.map(t => `${t.name} ${t.score}`).join(" vs ")}`;

  } catch (error) {
    out.verification_status = "UNAVAILABLE";
    out.validation_notes = error.message;
  }

  return out;
}


// --------------------------------------------------
// MAIN
// --------------------------------------------------

async function main() {
  const [cmd, ...args] = process.argv.slice(2);
  const opt = {};

  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      opt[args[i].slice(2)] = args[++i];
    }
  }


  // GENERATE CSV

  if (cmd === "generate") {
    const manifest = JSON.parse(
      readFileSync(
        resolve(opt.manifest ?? DEFAULT_MANIFEST),
        "utf8"
      )
    );

    const rows = manifest.games.map(game => ({
      provider_game_id: game.providerGameId,
      season_id: "20212022",
      game_date: game.date,
      home_team: game.homeTeam,
      away_team: game.awayTeam,
      home_score: game.officialScore[0],
      away_score: game.officialScore[1],
      match_id: "",
      source_url: "",
      notes: "",
      verification_status: "UNVERIFIED",
    }));

    writeFileSync(
      resolve(opt.out ?? DEFAULT_CSV),
      [
        COLUMNS.join(","),
        ...rows.map(row =>
          COLUMNS.map(key => esc(row[key])).join(",")
        ),
      ].join("\r\n") + "\r\n"
    );

    console.log(`Generated ${rows.length} rows`);
    return;
  }


  const csv = resolve(opt.csv ?? DEFAULT_CSV);
  const rows = parseCsv(readFileSync(csv, "utf8"));

  const counts = new Map();

  for (const row of rows) {
    if (row.match_id.trim()) {
      counts.set(
        row.match_id.trim(),
        (counts.get(row.match_id.trim()) ?? 0) + 1
      );
    }
  }

  const duplicateIds = new Set(
    [...counts]
      .filter(([, count]) => count > 1)
      .map(([id]) => id)
  );


  // VALIDATE CSV

  if (cmd === "validate") {
    const result = [];

    for (const row of rows) {
      const validation = await validateMapping(row);

      if (duplicateIds.has(row.match_id.trim())) {
        validation.verification_status = "CONFLICT";
        validation.validation_notes +=
          `; duplicate match_id ${row.match_id.trim()}`;
      }

      row.verification_status =
        validation.verification_status;

      result.push(validation);

      console.log(
        `${validation.provider_game_id}: ` +
        `${validation.verification_status} ` +
        `${validation.validation_notes}`
      );
    }

    writeFileSync(
      csv,
      [
        COLUMNS.join(","),
        ...rows.map(row =>
          COLUMNS.map(key => esc(row[key])).join(",")
        ),
      ].join("\r\n") + "\r\n"
    );

    const report = resolve(
      opt.report ??
      csv.replace(/\.csv$/i, ".validation.json")
    );

    writeFileSync(
      report,
      JSON.stringify(
        {
          created_at: new Date().toISOString(),
          rows: result,
        },
        null,
        2
      )
    );

    console.log(`Report ${report}`);
    return;
  }


  // APPLY TO DISPOSABLE DATABASE ONLY

  if (cmd === "apply") {
    const dbPath = resolve(opt.database ?? "");

    if (
      !opt.database ||
      dbPath === resolve("data/arc2.db") ||
      basename(dbPath).toLowerCase() === "arc2.db"
    ) {
      throw Error(
        "Specify a disposable database; production arc2.db is forbidden"
      );
    }

    const verified = [];

    for (const row of rows) {
      if (!row.match_id) {
        continue;
      }

      const validation = duplicateIds.has(row.match_id.trim())
        ? {
            verification_status: "CONFLICT",
            validation_notes: "duplicate match_id in CSV",
          }
        : await validateMapping(row);

      if (validation.verification_status === "VERIFIED") {
        verified.push({
          ...row,
          ...validation,
        });
      } else {
        console.log(
          `${row.provider_game_id}: skipped ` +
          `${validation.verification_status}`
        );
      }
    }

    const db = new DatabaseSync(dbPath);

    try {
      db.exec(`
        PRAGMA foreign_keys=ON;
        BEGIN IMMEDIATE;

        INSERT OR IGNORE INTO seasons(
          season_id,
          label,
          start_year,
          end_year
        )
        VALUES(
          20212022,
          '2021-22',
          2021,
          2022
        );
      `);

      for (const row of verified) {
        const gameId = `estlatbl_${row.provider_game_id}`;

        const homeTeamId = resolveTeamId(db, row.home_team);
        const awayTeamId = resolveTeamId(db, row.away_team);

        db.prepare(`
          INSERT INTO games(
            game_id,
            league_id,
            season_id,
            game_date,
            home_team_id,
            away_team_id,
            home_score,
            away_score,
            status
          )
          VALUES(
            ?,
            1,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            'scheduled_or_import_pending'
          )
          ON CONFLICT(game_id) DO NOTHING
        `).run(
          gameId,
          20212022,
          row.game_date,
          homeTeamId,
          awayTeamId,
          Number(row.home_score),
          Number(row.away_score)
        );

        const conflict = db.prepare(`
          SELECT 1
          FROM source_livestats_games
          WHERE game_id = ?
             OR provider_game_id = ?
             OR match_id = ?
        `).get(
          gameId,
          row.provider_game_id,
          String(row.match_id)
        );

        if (conflict) {
          throw Error(
            `${row.provider_game_id}: crosswalk conflict/duplicate`
          );
        }

        db.prepare(`
          INSERT INTO source_livestats_games(
            game_id,
            league_id,
            season_id,
            provider_game_id,
            match_id,
            json_url
          )
          VALUES(
            ?,
            1,
            20212022,
            ?,
            ?,
            ?
          )
        `).run(
          gameId,
          row.provider_game_id,
          String(row.match_id),
          `https://fibalivestats.dcd.shared.geniussports.com/data/${row.match_id}/data.json`
        );
      }

      db.exec("COMMIT");

      console.log(
        `Applied ${verified.length} freshly validated mappings; ` +
        "retain CSV/report as manual provenance."
      );

    } catch (error) {
      try {
        db.exec("ROLLBACK");
      } catch {
        // Ignore rollback errors.
      }

      throw error;

    } finally {
      db.close();
    }

    return;
  }


  throw Error(
    "Usage: mapping_recovery.mjs generate | " +
    "validate [--csv file --report file] | " +
    "apply --csv file --database disposable.db"
  );
}


if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}