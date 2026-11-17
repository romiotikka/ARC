#!/usr/bin/env node
// Browser-level supervisor for 2021–22 mapping recovery.
// Navigation is driven through Chrome DevTools Protocol; response JSON is
// fetched by Runtime.evaluate only after the page has reached the FIBA origin.
import { readFileSync, writeFileSync, renameSync, mkdirSync, openSync, fsyncSync, closeSync, readdirSync, statSync, existsSync } from "node:fs";
import { dirname, basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const ESTLATBL_HOST = "www.estlatbl.com";
const FIBA_HOST = "fibalivestats.dcd.shared.geniussports.com";
const DEFAULT_MANIFEST = ".tmp-2021-22-mapping-audit-20261115/browser-batch/unattempted-games.json";
const DEFAULT_DIR = ".tmp-2021-22-mapping-audit-20261115/browser-batch";
const CHALLENGE = /cf-chl|challenge-platform|cloudflare|just a moment|turvakontroll|turnstile/i;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class SafeBatchStop extends Error {
  constructor(status, message) {
    super(message);
    this.name = "SafeBatchStop";
    this.status = status;
  }
}

export function extractMatchIdFromFinalUrl(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== "https:" || url.hostname !== FIBA_HOST) return null;
  return url.pathname.match(/^\/u\/EBF\/(\d+)(?:\/|$)/i)?.[1]
    ?? url.searchParams.get("matchId")
    ?? null;
}

export function isChallengeResponse(status, body = "") {
  return status === 403 || status === 429 || CHALLENGE.test(String(body));
}

export function parseBatchLimit(value) {
  const limit = Number(value || 20);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new Error("--limit must be an integer from 1 to 100");
  }
  return limit;
}

export function makeFibaCaptureExpression(matchId) {
  if (!/^\d+$/.test(String(matchId))) throw new Error("FIBA matchId must contain digits only");
  const id = JSON.stringify(String(matchId));
  return `(async()=>{
    const expectedOrigin=${JSON.stringify(`https://${FIBA_HOST}`)};
    if(location.origin!==expectedOrigin) throw new Error('FIBA origin guard failed: '+location.origin);
    const id=${id};
    const read=async path=>{try{const response=await fetch(path,{credentials:'same-origin',cache:'no-store'});return {status:response.status,contentType:response.headers.get('content-type')||'',text:await response.text(),error:null};}catch(error){return {status:null,contentType:'',text:'',error:String(error?.message||error)};}};
    const [json,page]=await Promise.all([read('/data/'+encodeURIComponent(id)+'/data.json'),read('/u/EBF/'+encodeURIComponent(id)+'/p.html')]);
    return {currentUrl:location.href,json,page};
  })()`;
}

class CdpConnection {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Map();
    socket.addEventListener("message", (event) => {
      let message;
      try { message = JSON.parse(String(event.data)); } catch { return; }
      if (message.id && this.pending.has(message.id)) {
        const item = this.pending.get(message.id);
        clearTimeout(item.timer);
        this.pending.delete(message.id);
        if (message.error) item.reject(new Error(message.error.message || "CDP command failed"));
        else item.resolve(message.result || {});
        return;
      }
      const callbacks = this.listeners.get(message.method) || [];
      for (const callback of [...callbacks]) callback(message.params || {});
    });
    socket.addEventListener("close", () => {
      for (const item of this.pending.values()) {
        clearTimeout(item.timer);
        item.reject(new SafeBatchStop("NAVIGATION_STOPPED", "Chrome DevTools Protocol connection closed"));
      }
      this.pending.clear();
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolveOpen, reject) => {
      const timer = setTimeout(() => reject(new Error("Timed out connecting to Chrome tab")), 10000);
      socket.addEventListener("open", () => { clearTimeout(timer); resolveOpen(); }, { once: true });
      socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error("Could not connect to Chrome tab")); }, { once: true });
    });
    return new CdpConnection(socket);
  }

  send(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolveCommand, rejectCommand) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        rejectCommand(new Error(`Chrome did not answer ${method} within 20 seconds`));
      }, 20000);
      this.pending.set(id, { resolve: resolveCommand, reject: rejectCommand, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  once(method, timeoutMs = 30000) {
    return new Promise((resolveEvent, rejectEvent) => {
      const callback = (params) => {
        clearTimeout(timer);
        const current = this.listeners.get(method) || [];
        this.listeners.set(method, current.filter((candidate) => candidate !== callback));
        resolveEvent(params);
      };
      const timer = setTimeout(() => {
        const current = this.listeners.get(method) || [];
        this.listeners.set(method, current.filter((candidate) => candidate !== callback));
        rejectEvent(new Error(`Timed out waiting for ${method}`));
      }, timeoutMs);
      this.listeners.set(method, [...(this.listeners.get(method) || []), callback]);
    });
  }

  close() { this.socket.close(); }
}

async function localJson(url) {
  const parsed = new URL(url);
  if (parsed.hostname !== "127.0.0.1" && parsed.hostname !== "localhost" && parsed.hostname !== "[::1]") {
    throw new Error("Chrome DevTools endpoint must be on localhost");
  }
  const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
  if (!response.ok) throw new Error(`Chrome DevTools endpoint HTTP ${response.status}`);
  return response.json();
}

export function isEstlatblPageTarget(target) {
  if (target?.type !== "page") return false;
  try {
    const url = new URL(target.url);
    return url.protocol === "https:" && ["www.estlatbl.com", "estlatbl.com"].includes(url.hostname);
  } catch {
    return false;
  }
}

export function chooseTargetFromList(targets) {
  if (!Array.isArray(targets)) return { target: null, reason: "target response was not an array" };
  const page = targets.find(isEstlatblPageTarget);
  if (!page) return { target: null, reason: `no HTTPS EstLatBL page among ${targets.length} targets` };
  if (!page.webSocketDebuggerUrl) {
    return { target: null, reason: `EstLatBL page ${page.id || "(no id)"} was listed but had no webSocketDebuggerUrl` };
  }
  return { target: page, reason: null };
}

export async function chooseTarget(cdpHttp, loadTargets = localJson) {
  const diagnostics = [];
  // /json is the endpoint the operator confirmed; retain /json/list as a
  // compatibility fallback for Chrome versions exposing that route only.
  for (const path of ["/json", "/json/list"]) {
    try {
      const targets = await loadTargets(new URL(path, cdpHttp).href);
      const choice = chooseTargetFromList(targets);
      if (choice.target) return choice.target;
      diagnostics.push(`${path}: ${choice.reason}`);
    } catch (error) {
      diagnostics.push(`${path}: ${error.message}`);
    }
  }
  throw new Error(`No open EstLatBL page with a CDP WebSocket was found. ${diagnostics.join("; ")}`);
}

async function evaluate(connection, expression) {
  const response = await connection.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: false,
  });
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text || "Page evaluation failed");
  }
  return response.result?.value;
}

async function navigate(connection, url) {
  const loaded = connection.once("Page.loadEventFired", 35000);
  let result;
  try { result = await connection.send("Page.navigate", { url }); }
  catch (error) { throw new SafeBatchStop("NAVIGATION_STOPPED", `Browser navigation command failed: ${error.message}`); }
  if (result.errorText) throw new SafeBatchStop("NAVIGATION_STOPPED", `Navigation failed: ${result.errorText}`);
  try { await loaded; }
  catch (error) { throw new SafeBatchStop("NAVIGATION_STOPPED", `Browser navigation did not finish: ${error.message}`); }
  await sleep(500);
  let page;
  try {
    page = await evaluate(connection, "({url:location.href,readyState:document.readyState,title:document.title,bodyText:(document.body?.innerText||'').slice(0,5000)})");
  } catch (error) {
    throw new SafeBatchStop("NAVIGATION_STOPPED", `Could not inspect the navigated page: ${error.message}`);
  }
  if (isChallengeResponse(200, `${page?.title || ""}\n${page?.bodyText || ""}`)) {
    throw new SafeBatchStop("CHALLENGE_STOPPED", "A browser security/challenge page was reached; no challenge was solved or bypassed.");
  }
  return page;
}

async function renameWithRetry(source, destination, rename = renameSync, retryDelays = [100, 250, 500, 1000]) {
  let lastError;
  for (let attempt = 0; attempt <= retryDelays.length; attempt += 1) {
    try {
      rename(source, destination);
      return;
    } catch (error) {
      lastError = error;
      if (!new Set(["EPERM", "EBUSY", "EACCES", "EEXIST"]).has(error.code) || attempt === retryDelays.length) throw error;
      await sleep(retryDelays[attempt]);
    }
  }
  throw lastError;
}

export async function atomicJson(path, value, { rename = renameSync, retryDelays } = {}) {
  mkdirSync(dirname(path), { recursive: true });
  const tempPath = `${path}.${process.pid}.${Date.now()}.${randomUUID()}.tmp`;
  const serialized = `${JSON.stringify(value, null, 2)}\n`;
  JSON.parse(serialized); // Validate before creating/replacing any destination.
  const fd = openSync(tempPath, "wx");
  try {
    writeFileSync(fd, serialized, "utf8");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  JSON.parse(readFileSync(tempPath, "utf8")); // Confirm the staged file is complete and valid.
  try {
    await renameWithRetry(tempPath, path, rename, retryDelays);
  } catch (error) {
    throw new Error(`Could not replace ${path}; the old destination was left in place and the validated staged JSON remains at ${tempPath}: ${error.code || error.message}`);
  }
  JSON.parse(readFileSync(path, "utf8")); // Verify the committed destination before reporting success.
  return path;
}

export function readBestCheckpoint(path, expectedManifestPath) {
  const candidates = [];
  if (existsSync(path)) {
    try {
      const state = JSON.parse(readFileSync(path, "utf8"));
      if (!expectedManifestPath || state.manifestPath === expectedManifestPath) {
        candidates.push({ state, mtime: statSync(path).mtimeMs, source: path });
      }
    } catch { /* Preserve an invalid checkpoint and consider validated staged copies below. */ }
  }
  const prefix = `${basename(path)}.`;
  for (const name of readdirSync(dirname(path))) {
    if (!name.startsWith(prefix) || !name.endsWith(".tmp")) continue;
    const candidatePath = join(dirname(path), name);
    try {
      const state = JSON.parse(readFileSync(candidatePath, "utf8"));
      if (!expectedManifestPath || state.manifestPath === expectedManifestPath) {
        candidates.push({ state, mtime: statSync(candidatePath).mtimeMs, source: candidatePath });
      }
    } catch { /* Keep malformed staged files for inspection; do not adopt them. */ }
  }
  candidates.sort((a, b) =>
    Object.keys(b.state.results || {}).length - Object.keys(a.state.results || {}).length ||
    Object.keys(b.state.evidence || {}).length - Object.keys(a.state.evidence || {}).length ||
    b.mtime - a.mtime,
  );
  return candidates[0] || null;
}

function outputBundles(manifest, results, evidence) {
  return {
    candidates: {
      exportedAt: new Date().toISOString(),
      seasonId: manifest.seasonId,
      verifiedMappings: manifest.verifiedMappings || [],
      preservedUnverified: manifest.preservedUnverified || [],
      games: manifest.games,
      results: Object.values(results),
    },
    evidence: {
      exportedAt: new Date().toISOString(),
      seasonId: manifest.seasonId,
      evidence: Object.values(evidence),
    },
  };
}

async function main(argv = process.argv.slice(2)) {
  const options = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--help") {
      console.log("node .tmp-2021-22-mapping-audit-20261115/browser-batch/navigate-and-capture.mjs [--cdp http://127.0.0.1:9222] [--manifest path] [--checkpoint path] [--limit 1..100] [--retry PROVIDER_GAME_ID] [--export-only]");
      return;
    }
    if (argv[i] === "--export-only") { options.exportOnly = true; continue; }
    if (argv[i] === "--retry") {
      options.retry ||= [];
      options.retry.push(String(argv[++i]));
    } else if (argv[i].startsWith("--")) options[argv[i].slice(2)] = argv[++i];
  }
  const limit = parseBatchLimit(options.limit);
  const cdpHttp = options.cdp || "http://127.0.0.1:9222";
  const manifestPath = resolve(options.manifest || DEFAULT_MANIFEST);
  const checkpointPath = resolve(options.checkpoint || `${DEFAULT_DIR}/navigation-checkpoint.json`);
  const candidatesPath = resolve(`${DEFAULT_DIR}/arc-2021-22-live-stats-candidates.json`);
  const evidencePath = resolve(`${DEFAULT_DIR}/arc-2021-22-fiba-evidence.json`);
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  if (manifest.seasonId !== "20212022" || !Array.isArray(manifest.games)) throw new Error("Manifest must be the 2021–22 unresolved-game manifest");

  const recovered = readBestCheckpoint(checkpointPath, manifestPath);
  let state = recovered?.state;
  if (state && state.manifestPath !== manifestPath) throw new Error("Checkpoint belongs to a different manifest; choose a separate --checkpoint path");
  state ||= { manifestPath, results: {}, evidence: {}, priorAttempts: {} };
  if (recovered && recovered.source !== checkpointPath) {
    console.warn(`Recovered the newest valid checkpoint from staged file ${recovered.source}; the staged file was preserved.`);
  }
  state.priorAttempts ||= {};
  for (const id of options.retry || []) {
    const prior = state.results[id];
    if (!prior) continue;
    state.priorAttempts[id] ||= [];
    state.priorAttempts[id].push({
      attemptedAt: prior.attemptedAt,
      completedAt: prior.completedAt,
      status: prior.status,
      matchId: prior.matchId,
      finalUrl: prior.finalUrl,
      error: prior.error,
    });
    delete state.results[id];
  }
  if (state.pausedProviderGameId) {
    const id = String(state.pausedProviderGameId);
    const prior = state.results[id];
    if (prior) {
      state.priorAttempts[id] ||= [];
      state.priorAttempts[id].push({
        attemptedAt: prior.attemptedAt,
        completedAt: prior.completedAt,
        status: prior.status,
        matchId: prior.matchId,
        finalUrl: prior.finalUrl,
        error: prior.error,
      });
      if (prior.matchId && state.evidence[prior.matchId]?.error) {
        state.priorEvidence ||= [];
        state.priorEvidence.push(state.evidence[prior.matchId]);
        delete state.evidence[prior.matchId];
      }
      delete state.results[id];
    }
    state.pausedProviderGameId = null;
    state.pauseReason = null;
  }

  const exportBundles = async () => {
    const bundles = outputBundles(manifest, state.results, state.evidence);
    await atomicJson(candidatesPath, bundles.candidates);
    await atomicJson(evidencePath, bundles.evidence);
    console.log(`Review bundles updated from checkpoint: ${Object.keys(state.results).length} results; ${Object.keys(state.evidence).length} evidence records.`);
  };
  if (options.exportOnly) {
    await exportBundles();
    return;
  }
  const verifiedOwners = new Map((manifest.verifiedMappings || []).map((item) => [String(item.matchId), String(item.providerGameId)]));
  for (const item of manifest.preservedUnverified || []) {
    if (item.matchId) verifiedOwners.set(String(item.matchId), String(item.providerGameId));
  }
  for (const record of Object.values(state.results)) {
    if (record.matchId && record.status === "CANDIDATE_EXTRACTED_UNVERIFIED") verifiedOwners.set(String(record.matchId), String(record.providerGameId));
  }

  const version = await localJson(new URL("/json/version", cdpHttp).href).catch((error) => {
    throw new Error(`Cannot reach Chrome DevTools at ${cdpHttp}. Start Chrome with a localhost remote-debugging port, then retry. (${error.message})`);
  });
  if (!version.webSocketDebuggerUrl) throw new Error("Chrome DevTools version response did not include a WebSocket endpoint");
  const target = await chooseTarget(cdpHttp);
  const connection = await CdpConnection.connect(target.webSocketDebuggerUrl);
  try {
    await connection.send("Page.enable");
    await connection.send("Runtime.enable");
    let processed = 0;
    let stopped = false;
    for (const game of manifest.games) {
      const providerGameId = String(game.providerGameId);
      if (state.results[providerGameId]) continue;
      if (processed >= limit) break;
      processed += 1;
      const attemptedAt = new Date().toISOString();
      const officialUrl = `https://${ESTLATBL_HOST}/et/tulemused/${encodeURIComponent(providerGameId)}/live-stats`;
      const record = {
        providerGameId,
        gameDate: game.gameDate,
        homeTeam: game.homeTeam,
        awayTeam: game.awayTeam,
        homeScore: game.homeScore,
        awayScore: game.awayScore,
        sourceUrl: officialUrl,
        attemptedAt,
        completedAt: null,
        status: "UNAVAILABLE",
        matchId: null,
        finalUrl: null,
        error: null,
        priorAttempts: state.priorAttempts[providerGameId] || [],
      };
      try {
        const finalPage = await navigate(connection, officialUrl);
        record.finalUrl = finalPage?.url || null;
        const matchId = extractMatchIdFromFinalUrl(record.finalUrl || "");
        if (!matchId) {
          throw new SafeBatchStop("NAVIGATION_STOPPED", `Final page was not the expected HTTPS FIBA URL: ${record.finalUrl || "unavailable"}`);
        } else {
          record.matchId = matchId;
          const owner = verifiedOwners.get(matchId);
          if (owner && owner !== providerGameId) {
            record.status = "DUPLICATE_MATCH_ID_CONFLICT";
            record.error = `FIBA matchId ${matchId} is already held by provider game ${owner}; existing mapping was not changed.`;
          } else {
            record.status = "CANDIDATE_EXTRACTED_UNVERIFIED";
            verifiedOwners.set(matchId, providerGameId);
            let capture;
            try { capture = await evaluate(connection, makeFibaCaptureExpression(matchId)); }
            catch (error) { throw new SafeBatchStop("NAVIGATION_STOPPED", `FIBA page context was lost during evidence capture: ${error.message}`); }
            const entry = {
              matchId,
              providerGameIds: [providerGameId],
              sourceUrl: `https://${FIBA_HOST}/u/EBF/${matchId}/p.html`,
              finalUrl: capture?.currentUrl || record.finalUrl,
              jsonUrl: `https://${FIBA_HOST}/data/${matchId}/data.json`,
              capturedAt: new Date().toISOString(),
              jsonStatus: capture?.json?.status ?? null,
              pageStatus: capture?.page?.status ?? null,
              dataJsonText: capture?.json?.text ?? null,
              pageHtml: capture?.page?.text ?? null,
              error: null,
            };
            if (isChallengeResponse(entry.jsonStatus, entry.dataJsonText) || isChallengeResponse(entry.pageStatus, entry.pageHtml)) {
              entry.error = "FIBA returned a security/challenge response; no challenge was solved or bypassed.";
              record.status = "CHALLENGE_STOPPED";
              stopped = true;
            } else if (capture?.json?.error || capture?.page?.error) {
              entry.error = `Single-game FIBA fetch failure: data.json=${capture?.json?.error || "ok"}; p.html=${capture?.page?.error || "ok"}`;
            } else if (entry.jsonStatus !== 200 || entry.pageStatus !== 200) {
              entry.error = `Incomplete FIBA evidence: data.json HTTP ${entry.jsonStatus}; p.html HTTP ${entry.pageStatus}`;
            }
            state.evidence[matchId] = entry;
            if (entry.error) record.error = entry.error;
          }
        }
      } catch (error) {
        record.status = error instanceof SafeBatchStop ? error.status : "UNAVAILABLE";
        record.error = String(error?.message || error);
        if (error instanceof SafeBatchStop) stopped = true;
      }
      record.completedAt = new Date().toISOString();
      state.results[providerGameId] = record;
      if (stopped) {
        state.pausedProviderGameId = providerGameId;
        state.pauseReason = record.error;
      }
      await atomicJson(checkpointPath, state);
      console.log(`${providerGameId}: ${record.status}${record.matchId ? ` (${record.matchId})` : ""}${record.error ? ` — ${record.error}` : ""}`);
      if (stopped) {
        break;
      }
      await sleep(900);
    }
    await exportBundles();
    const untouched = manifest.games.filter((game) => !state.results[String(game.providerGameId)]).length;
    const extracted = Object.values(state.results).filter((record) => record.matchId).length;
    const evidenceComplete = Object.values(state.evidence).filter((entry) => entry.jsonStatus === 200 && entry.pageStatus === 200 && !entry.error).length;
    console.log(JSON.stringify({
      batchAttempted: processed,
      totalGames: manifest.games.length,
      matchIdsExtracted: extracted,
      completeEvidenceRecords: evidenceComplete,
      untouchedGames: untouched,
      pausedProviderGameId: state.pausedProviderGameId || null,
      checkpoint: checkpointPath,
      candidates: candidatesPath,
      evidence: evidencePath,
      note: "Candidates remain unverified until the unchanged offline ARC validator runs.",
    }, null, 2));
  } finally {
    connection.close();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
