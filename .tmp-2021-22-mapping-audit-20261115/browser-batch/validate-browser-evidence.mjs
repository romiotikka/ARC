#!/usr/bin/env node
// Offline bridge: feeds browser-captured FIBA responses to ARC's existing
// validateMapping() implementation, then writes a merge-ready CSV and report.
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { validateMapping } from "../../scripts/leagues/estlatbl/mapping_recovery.mjs";

const COLUMNS = ["provider_game_id","season_id","game_date","home_team","away_team","home_score","away_score","match_id","source_url","notes","verification_status"];
function parseCsv(text) {
  const rows = []; let row = []; let cell = ""; let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i += 1; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
    else if (c !== "\r") cell += c;
  }
  if (quoted) throw new Error("Malformed CSV quoting");
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const [header, ...body] = rows;
  if (JSON.stringify(header) !== JSON.stringify(COLUMNS)) throw new Error(`CSV header mismatch; expected ${COLUMNS.join(",")}`);
  return body.filter((r) => r.some(Boolean)).map((r) => Object.fromEntries(COLUMNS.map((k, i) => [k, r[i] ?? ""])));
}
const csvCell = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
function argsOf(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--help") out.help = true;
    else if (argv[i].startsWith("--")) out[argv[i].slice(2)] = argv[++i];
  }
  return out;
}

async function main() {
  const args = argsOf(process.argv.slice(2));
  if (args.help) {
    console.log("node .tmp-2021-22-mapping-audit-20261115/browser-batch/validate-browser-evidence.mjs --csv data/estlatbl_2021_22_mapping_overrides.csv --candidates arc-2021-22-live-stats-candidates.json --evidence arc-2021-22-fiba-evidence.json --out candidates-validated.csv --report candidates-validation.json");
    return;
  }
  for (const name of ["csv", "candidates", "evidence", "out", "report"]) if (!args[name]) throw new Error(`Missing --${name}; use --help`);
  const rows = parseCsv(readFileSync(resolve(args.csv), "utf8"));
  const candidates = JSON.parse(readFileSync(resolve(args.candidates), "utf8"));
  const bundle = JSON.parse(readFileSync(resolve(args.evidence), "utf8"));
  if (candidates.seasonId !== "20212022" || bundle.seasonId !== "20212022") throw new Error("Browser evidence must be for 2021–22.");
  const evidenceById = new Map((bundle.evidence || []).map((item) => [String(item.matchId), item]));
  const candidateRows = [
    ...(candidates.results || []).filter((item) => item.matchId).map((item) => ({ ...item, sourceKind: "collector" })),
    ...(candidates.preservedUnverified || []).map((item) => ({ ...item, status: "PRESERVED_UNVERIFIED" })),
  ];
  const rowsByGame = new Map(rows.map((r) => [String(r.provider_game_id), r]));
  const candidateIds = new Set();
  const issues = [];
  for (const candidate of candidateRows) {
    const gameId = String(candidate.providerGameId);
    const row = rowsByGame.get(gameId);
    if (!row) { issues.push({ provider_game_id: gameId, status: "CONFLICT", reason: "provider game is absent from base CSV" }); continue; }
    if (row.verification_status === "VERIFIED") continue; // Never downgrade an existing verified mapping.
    const incomingId = String(candidate.matchId || "").trim();
    if (!incomingId) continue;
    if (row.match_id && row.match_id !== incomingId) {
      row.verification_status = "CONFLICT";
      issues.push({ provider_game_id: gameId, match_id: incomingId, status: "CONFLICT", reason: `base CSV already has a different candidate match_id ${row.match_id}` });
      continue;
    }
    row.match_id = incomingId;
    row.source_url ||= candidate.sourceUrl || candidate.source_url || `https://www.estlatbl.com/et/tulemused/${gameId}/live-stats`;
    if (candidate.error || candidate.status === "DUPLICATE_MATCH_ID_CONFLICT") {
      row.verification_status = candidate.status === "DUPLICATE_MATCH_ID_CONFLICT" ? "CONFLICT" : "UNVERIFIED";
      issues.push({ provider_game_id: gameId, match_id: incomingId, status: row.verification_status, reason: candidate.error || "collection produced an incomplete candidate" });
    }
    candidateIds.add(incomingId);
  }

  const owners = new Map();
  for (const row of rows) {
    const id = String(row.match_id || "").trim();
    if (!id) continue;
    if (!owners.has(id)) owners.set(id, []);
    owners.get(id).push(row);
  }
  const reportRows = [];
  for (const id of candidateIds) {
    const matches = owners.get(id) || [];
    if (matches.length > 1) {
      for (const row of matches) {
        if (row.verification_status !== "VERIFIED") row.verification_status = "CONFLICT";
        reportRows.push({ provider_game_id: row.provider_game_id, match_id: id, verification_status: "CONFLICT", validation_notes: `duplicate FIBA match_id is assigned to ${matches.map((x) => x.provider_game_id).join(", ")}` });
      }
      continue;
    }
    const row = matches[0];
    if (!row || row.verification_status === "CONFLICT") continue;
    const evidence = evidenceById.get(id);
    if (!evidence) {
      row.verification_status = "UNVERIFIED";
      reportRows.push({ provider_game_id: row.provider_game_id, match_id: id, verification_status: "UNVERIFIED", validation_notes: "FIBA response evidence is missing" });
      continue;
    }
    if (evidence.error || evidence.jsonStatus !== 200 || evidence.pageStatus !== 200 || !evidence.dataJsonText || !evidence.pageHtml) {
      row.verification_status = "UNVERIFIED";
      reportRows.push({ provider_game_id: row.provider_game_id, match_id: id, verification_status: "UNVERIFIED", transportStatus: "UNAVAILABLE_OR_INCOMPLETE", validation_notes: evidence.error || `incomplete HTTP evidence: json=${evidence.jsonStatus}; p.html=${evidence.pageStatus}; body_present=${Boolean(evidence.dataJsonText)}/${Boolean(evidence.pageHtml)}`, capturedAt: evidence.capturedAt, sourceUrl: evidence.sourceUrl, jsonUrl: evidence.jsonUrl });
      continue;
    }
    try { JSON.parse(evidence.dataJsonText); }
    catch {
      row.verification_status = "UNVERIFIED";
      reportRows.push({ provider_game_id: row.provider_game_id, match_id: id, verification_status: "UNVERIFIED", transportStatus: "INCOMPLETE_JSON", validation_notes: "captured JSON body is not complete valid JSON", capturedAt: evidence.capturedAt, sourceUrl: evidence.sourceUrl, jsonUrl: evidence.jsonUrl });
      continue;
    }
    const fetcher = async (url) => {
      if (evidence.error) throw new Error(evidence.error);
      if (url.includes(`/data/${encodeURIComponent(id)}/data.json`)) {
        if (!evidence.dataJsonText) throw new Error("captured JSON body is missing");
        return new Response(evidence.dataJsonText, { status: evidence.jsonStatus || 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes(`/u/EBF/${encodeURIComponent(id)}/p.html`)) {
        if (!evidence.pageHtml) throw new Error("captured p.html body is missing");
        return new Response(evidence.pageHtml, { status: evidence.pageStatus || 200, headers: { "content-type": "text/html" } });
      }
      throw new Error(`unexpected validator URL: ${url}`);
    };
    const checked = await validateMapping(row, fetcher);
    const finalStatus = checked.verification_status === "UNAVAILABLE" ? "UNVERIFIED" : checked.verification_status;
    row.verification_status = finalStatus;
    reportRows.push({ provider_game_id: row.provider_game_id, match_id: id, verification_status: finalStatus, transportStatus: checked.verification_status === "UNAVAILABLE" ? "UNAVAILABLE" : "OK", validation_notes: checked.validation_notes, capturedAt: evidence.capturedAt, sourceUrl: evidence.sourceUrl, jsonUrl: evidence.jsonUrl });
  }

  writeFileSync(resolve(args.out), `${COLUMNS.join(",")}\r\n${rows.map((r) => COLUMNS.map((k) => csvCell(r[k])).join(",")).join("\r\n")}\r\n`);
  writeFileSync(resolve(args.report), JSON.stringify({ generatedAt: new Date().toISOString(), sourceCsv: resolve(args.csv), candidateBundle: resolve(args.candidates), evidenceBundle: resolve(args.evidence), summary: Object.fromEntries(["VERIFIED", "CONFLICT", "UNVERIFIED", "UNAVAILABLE"].map((s) => [s, reportRows.filter((r) => r.verification_status === s).length])), rows: reportRows, collectionIssues: issues }, null, 2));
  console.log(`Wrote merge-ready CSV: ${resolve(args.out)}`);
  console.log(`Wrote validation report: ${resolve(args.report)}`);
  console.log(JSON.stringify(Object.fromEntries(["VERIFIED", "CONFLICT", "UNVERIFIED", "UNAVAILABLE"].map((s) => [s, reportRows.filter((r) => r.verification_status === s).length])), null, 2));
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
