// Paste into DevTools Console on an ordinary public FIBA LiveStats page at
// https://fibalivestats.dcd.shared.geniussports.com (not on estlatbl.com).
// This reads same-origin JSON and p.html only; ARC's Node validator makes the
// VERIFIED/CONFLICT decision offline after exporting this evidence bundle.
(() => {
  const DB_NAME = "arc-estlatbl-2021-22-fiba-evidence-v1";
  const STORE = "evidence";
  const BATCH_SIZE = 15;
  const DELAY_MS = 1400;
  const CHALLENGE = /cf-chl|challenge-platform|cloudflare|just a moment|turvakontroll|turnstile/i;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const chooseJson = () => new Promise((resolve, reject) => {
    const input = document.createElement("input"); input.type = "file";
    input.accept = ".json,application/json";
    input.onchange = async () => { try { resolve(JSON.parse(await input.files[0].text())); } catch (e) { reject(e); } };
    input.click();
  });
  const openDb = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "matchId" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const get = (db, key) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly"); const request = tx.objectStore(STORE).get(key);
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
  const put = (db, value) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite"); tx.objectStore(STORE).put(value);
    tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error);
  });
  const remove = (db, key) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite"); tx.objectStore(STORE).delete(key);
    tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error);
  });
  const all = (db) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly"); const request = tx.objectStore(STORE).getAll();
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
  const saveState = (db, state) => new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put({ matchId: "__state__", state });
    tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error);
  });
  const readState = async (db) => (await get(db, "__state__"))?.state || null;
  const downloadJson = (filename, value) => {
    const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
    const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = filename; link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 30000);
  };
  const detectChallenge = (response, body) => response.status === 403 || response.status === 429 || CHALLENGE.test(body);
  const run = async () => {
    if (location.hostname !== "fibalivestats.dcd.shared.geniussports.com") {
      throw new Error("Open this console on an ordinary FIBA LiveStats page first.");
    }
    const db = await openDb();
    let state = await readState(db);
    if (!state) {
      const source = await chooseJson();
      if (source.seasonId !== "20212022" || !Array.isArray(source.games) || !Array.isArray(source.results)) {
        throw new Error("Selected file is not the ARC 2021–22 browser candidate export.");
      }
      const candidates = [
        ...(source.results.filter((r) => r.matchId).map((r) => ({ ...r, sourceKind: "collector" }))),
        ...(source.preservedUnverified || []).map((r) => ({ ...r, sourceKind: "preserved_unverified" })),
      ];
      const byId = new Map();
      for (const c of candidates) {
        const matchId = String(c.matchId || "").trim();
        if (!matchId) continue;
        const prev = byId.get(matchId);
        if (!prev) byId.set(matchId, { matchId, providerGameIds: [String(c.providerGameId)], candidates: [c] });
        else {
          if (!prev.providerGameIds.includes(String(c.providerGameId))) prev.providerGameIds.push(String(c.providerGameId));
          prev.candidates.push(c);
        }
      }
      state = { seasonId: source.seasonId, source, matches: [...byId.values()], pausedOnChallenge: null, priorAttempts: {} };
      await saveState(db, state);
    }
    if (state.pausedOnChallenge) {
      throw new Error(`Paused at FIBA matchId ${state.pausedOnChallenge}. Inspect access; after it is legitimately available, call ARC2021FibaEvidence.resumeAfterChallenge().`);
    }
    let processed = 0; let stopped = false;
    for (const item of state.matches) {
      if (await get(db, item.matchId)) continue;
      if (processed >= BATCH_SIZE) break;
      processed += 1;
      const capturedAt = new Date().toISOString();
      const rec = { matchId: item.matchId, providerGameIds: item.providerGameIds,
        sourceUrl: `https://fibalivestats.dcd.shared.geniussports.com/u/EBF/${encodeURIComponent(item.matchId)}/p.html`,
        jsonUrl: `https://fibalivestats.dcd.shared.geniussports.com/data/${encodeURIComponent(item.matchId)}/data.json`,
        capturedAt, jsonStatus: null, pageStatus: null, dataJsonText: null, pageHtml: null, error: null,
        priorAttempts: state.priorAttempts?.[item.matchId] || [] };
      try {
        const jr = await fetch(rec.jsonUrl, { credentials: "same-origin", cache: "no-store" });
        rec.jsonStatus = jr.status;
        rec.dataJsonText = await jr.text();
        if (detectChallenge(jr, rec.dataJsonText)) {
          rec.error = `Challenge/security response from JSON endpoint (HTTP ${jr.status}).`;
          state.pausedOnChallenge = item.matchId; await put(db, rec); await saveState(db, state); stopped = true; break;
        }
        if (!jr.ok) throw new Error(`JSON endpoint HTTP ${jr.status}`);
        await sleep(600);
        const pr = await fetch(rec.sourceUrl, { credentials: "same-origin", cache: "no-store" });
        rec.pageStatus = pr.status;
        rec.pageHtml = await pr.text();
        if (detectChallenge(pr, rec.pageHtml)) {
          rec.error = `Challenge/security response from p.html endpoint (HTTP ${pr.status}).`;
          state.pausedOnChallenge = item.matchId; await put(db, rec); await saveState(db, state); stopped = true; break;
        }
        if (!pr.ok) throw new Error(`p.html endpoint HTTP ${pr.status}`);
      } catch (error) {
        rec.error = String(error?.message || error);
      }
      await put(db, rec); // Durable IndexedDB checkpoint after every matchId.
      await saveState(db, state);
      console.log(`${item.matchId}: ${rec.error ? "EVIDENCE_INCOMPLETE" : "EVIDENCE_CAPTURED"} for ${item.providerGameIds.join(", ")}`);
      if (processed < BATCH_SIZE) await sleep(DELAY_MS);
    }
    const captured = (await all(db)).filter((r) => r.matchId !== "__state__");
    const completedIds = new Set(captured.map((r) => r.matchId));
    const pending = state.matches.filter((item) => !completedIds.has(item.matchId)).length;
    console.log(`Batch complete: ${processed} FIBA match IDs attempted; ${captured.length} evidence records saved. ${stopped ? "Paused on a security/challenge response." : `Remaining match IDs: ${pending}.`}`);
    window.ARC2021FibaEvidence = {
      progress: async () => ({ attempted: (await all(db)).filter((r) => r.matchId !== "__state__").length, total: state.matches.length, pausedOnChallenge: state.pausedOnChallenge }),
      resumeAfterChallenge: async () => {
        state = await readState(db);
        if (!state?.pausedOnChallenge) return false;
        const id = state.pausedOnChallenge;
        const prior = await get(db, id);
        state.priorAttempts ||= {};
        state.priorAttempts[id] ||= [];
        if (prior) state.priorAttempts[id].push({ capturedAt: prior.capturedAt, jsonStatus: prior.jsonStatus, pageStatus: prior.pageStatus, error: prior.error });
        await remove(db, id);
        state.pausedOnChallenge = null; await saveState(db, state); return run();
      },
      retry: async (matchId) => {
        const id = String(matchId); const prior = await get(db, id);
        if (!prior) return false;
        state = await readState(db); state.priorAttempts ||= {}; state.priorAttempts[id] ||= [];
        state.priorAttempts[id].push({ capturedAt: prior.capturedAt, jsonStatus: prior.jsonStatus, pageStatus: prior.pageStatus, error: prior.error });
        await remove(db, id); await saveState(db, state); return true;
      },
      export: async () => downloadJson("arc-2021-22-fiba-evidence.json", {
        exportedAt: new Date().toISOString(), seasonId: state.seasonId,
        source: state.source, evidence: (await all(db)).filter((r) => r.matchId !== "__state__"),
      }),
    };
  };
  run().catch((error) => console.error("ARC FIBA evidence capture stopped:", error));
})();
