// Paste this into DevTools Console on https://www.estlatbl.com while using
// the already verified browser session. It fetches only official same-origin
// /live-stats pages and uses ARC's existing matchId=(\d+) extraction rule.
(() => {
  const KEY = "arc-estlatbl-2021-22-browser-collection-v1";
  const BATCH_SIZE = 20;
  const DELAY_MS = 1800;
  const CHALLENGE = /cf-chl|challenge-platform|cloudflare|just a moment|turvakontroll|turnstile/i;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const downloadJson = (filename, value) => {
    const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 30000);
  };
  const chooseJson = () => new Promise((resolve, reject) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.onchange = async () => {
      try { resolve(JSON.parse(await input.files[0].text())); }
      catch (error) { reject(error); }
    };
    input.click();
  });
  const saved = JSON.parse(localStorage.getItem(KEY) || "null") || { results: {} };
  const makeApi = () => ({
    run: start,
    export: () => {
      if (!saved.manifest) throw new Error("Select the unresolved-game manifest first.");
      return downloadJson("arc-2021-22-live-stats-candidates.json", {
        exportedAt: new Date().toISOString(), seasonId: saved.manifest.seasonId,
        verifiedMappings: saved.manifest.verifiedMappings || [],
        preservedUnverified: saved.manifest.preservedUnverified || [],
        games: saved.manifest.games, results: Object.values(saved.results),
      });
    },
    progress: () => ({ attempted: Object.keys(saved.results).length,
      total: saved.manifest?.games?.length || 0,
      unresolved: saved.manifest?.games?.filter((g) => !saved.results[String(g.providerGameId)]).length ?? null,
      pausedOnChallenge: saved.pausedOnChallenge || null }),
    resumeAfterChallenge: () => {
      if (!saved.pausedOnChallenge) return Promise.resolve(false);
      const id = String(saved.pausedOnChallenge);
      const prior = saved.results[id];
      saved.priorAttempts ||= {};
      saved.priorAttempts[id] ||= [];
      if (prior) saved.priorAttempts[id].push({ attemptedAt: prior.attemptedAt, completedAt: prior.completedAt, status: prior.status, httpStatus: prior.httpStatus, error: prior.error });
      delete saved.results[id];
      saved.pausedOnChallenge = null;
      localStorage.setItem(KEY, JSON.stringify(saved));
      return start();
    },
    retry: (providerGameId) => {
      const id = String(providerGameId); const prior = saved.results[id];
      if (!prior) return false;
      saved.priorAttempts ||= {}; saved.priorAttempts[id] ||= [];
      saved.priorAttempts[id].push({ attemptedAt: prior.attemptedAt, completedAt: prior.completedAt, status: prior.status, httpStatus: prior.httpStatus, error: prior.error });
      delete saved.results[id]; localStorage.setItem(KEY, JSON.stringify(saved)); return true;
    },
  });
  const start = async () => {
    if (saved.pausedOnChallenge) {
      window.ARC2021MappingCollection = makeApi();
      console.warn(`Paused after ${saved.pausedOnChallenge}. Inspect the page/session; after legitimate access is restored, call ARC2021MappingCollection.resumeAfterChallenge().`);
      return;
    }
    if (!new Set(["www.estlatbl.com", "estlatbl.com"]).has(location.hostname)) {
      throw new Error("Open this console on the official EstLatBL website first.");
    }
    if (!saved.manifest) {
      saved.manifest = await chooseJson();
      if (saved.manifest.seasonId !== "20212022" || !Array.isArray(saved.manifest.games)) {
        throw new Error("Selected file is not the ARC 2021–22 unresolved-game manifest.");
      }
      localStorage.setItem(KEY, JSON.stringify(saved));
    }
    const verifiedById = new Map((saved.manifest.verifiedMappings || []).map((x) => [String(x.matchId), x.providerGameId]));
    const preserved = saved.manifest.preservedUnverified || [];
    const knownOwners = new Map(verifiedById);
    for (const item of preserved) {
      if (item.matchId) knownOwners.set(String(item.matchId), item.providerGameId);
    }
    for (const record of Object.values(saved.results)) {
      if (record.matchId) knownOwners.set(String(record.matchId), record.providerGameId);
    }
    let processed = 0;
    let stop = false;
    for (const game of saved.manifest.games) {
      const key = String(game.providerGameId);
      if (saved.results[key]) continue; // Resume without repeating any attempted request.
      if (processed >= BATCH_SIZE) break;
      processed += 1;
      const attemptedAt = new Date().toISOString();
      const record = {
        providerGameId: key, gameDate: game.gameDate, homeTeam: game.homeTeam,
        awayTeam: game.awayTeam, homeScore: game.homeScore, awayScore: game.awayScore,
        sourceUrl: game.liveStatsUrl, attemptedAt, status: "UNAVAILABLE", matchId: null,
        httpStatus: null, error: null, completedAt: null,
        priorAttempts: saved.priorAttempts?.[key] || [],
      };
      try {
        const response = await fetch(game.liveStatsUrl, {
          credentials: "same-origin", redirect: "follow", cache: "no-store",
        });
        record.httpStatus = response.status;
        const body = await response.text();
        if (CHALLENGE.test(body) || response.status === 403 || response.status === 429) {
          record.status = "CHALLENGE_STOPPED";
          record.error = `Challenge/security response (HTTP ${response.status}); no challenge was solved or bypassed.`;
          saved.results[key] = record;
          saved.pausedOnChallenge = key;
          record.completedAt = new Date().toISOString();
          localStorage.setItem(KEY, JSON.stringify(saved));
          stop = true;
          console.warn(`Stopped at ${key}: challenge/security response.`);
          break;
        }
        if (!response.ok) {
          record.status = "UNAVAILABLE";
          record.error = `HTTP ${response.status}`;
        } else if (!/text\/html|application\/xhtml\+xml/i.test(response.headers.get("content-type") || "")) {
          record.status = "UNEXPECTED_RESPONSE_STOPPED";
          record.error = `Unexpected content-type: ${response.headers.get("content-type") || "missing"}`;
          saved.results[key] = record;
          saved.pausedOnChallenge = key;
          record.completedAt = new Date().toISOString();
          localStorage.setItem(KEY, JSON.stringify(saved));
          stop = true;
          console.warn(`Stopped at ${key}: unexpected response type.`);
          break;
        } else {
          const match = body.match(/matchId=(\d+)/); // Existing ARC collector parser.
          if (!match) {
            record.status = "NO_MATCH_ID";
            record.error = "Official page returned HTML but contained no matchId=(digits) value.";
          } else {
            record.matchId = match[1];
            const owner = knownOwners.get(record.matchId);
            if (owner && String(owner) !== key) {
              record.status = "DUPLICATE_MATCH_ID_CONFLICT";
              record.error = `matchId ${record.matchId} is already associated with provider game ${owner}; needs review.`;
            } else {
              record.status = "CANDIDATE_EXTRACTED_UNVERIFIED";
              knownOwners.set(record.matchId, key);
            }
          }
        }
      } catch (error) {
        record.status = "UNAVAILABLE";
        record.error = String(error?.message || error);
      }
      record.completedAt = new Date().toISOString();
      saved.results[key] = record;
      localStorage.setItem(KEY, JSON.stringify(saved)); // Checkpoint after every game.
      console.log(`${key}: ${record.status}${record.matchId ? ` (${record.matchId})` : ""}`);
      if (processed < BATCH_SIZE) await sleep(DELAY_MS);
    }
    const remaining = saved.manifest.games.filter((g) => !saved.results[String(g.providerGameId)]).length;
    console.table(Object.values(saved.results).slice(-processed).map(({ providerGameId, status, matchId, httpStatus }) => ({ providerGameId, status, matchId, httpStatus })));
    console.log(`Batch complete: ${processed} attempted; ${remaining} untouched. ${stop ? "Stopped for an unexpected/security response." : "Run this script again to resume, or export the current checkpoint."}`);
  };
  window.ARC2021MappingCollection = makeApi();
  start().catch((error) => console.error("ARC collection stopped:", error));
})();
