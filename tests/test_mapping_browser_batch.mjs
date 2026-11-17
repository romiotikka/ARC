import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  atomicJson,
  chooseTarget,
  chooseTargetFromList,
  extractMatchIdFromFinalUrl,
  isEstlatblPageTarget,
  isChallengeResponse,
  makeFibaCaptureExpression,
  parseBatchLimit,
  readBestCheckpoint,
} from "../.tmp-2021-22-mapping-audit-20261115/browser-batch/navigate-and-capture.mjs";
import { validateMapping } from "../scripts/leagues/estlatbl/mapping_recovery.mjs";

const row = {
  provider_game_id: "2022212040",
  season_id: "20212022",
  game_date: "2022-02-05",
  home_team: "BK Ventspils",
  away_team: "Latvijas Universitāte",
  home_score: "68",
  away_score: "59",
  match_id: "1957531",
  source_url: "https://www.estlatbl.com/et/tulemused/2022212040/live-stats",
  notes: "Preserve the existing source note.",
  verification_status: "UNVERIFIED",
};

test("discovers the supplied EstLatBL page from /json and falls back to /json/list", async () => {
  const page = {
    id: "04BE9CE5A6D5D9ED8C962C7B77227822",
    type: "page",
    title: "Tulemused",
    url: "https://www.estlatbl.com/et/tulemused?setSid=2022",
    webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/page/04BE9CE5A6D5D9ED8C962C7B77227822",
  };
  assert.equal(isEstlatblPageTarget(page), true);
  assert.equal(chooseTargetFromList([page]).target.id, page.id);
  assert.equal(chooseTargetFromList([{ ...page, webSocketDebuggerUrl: undefined }]).target, null);

  const requested = [];
  const selected = await chooseTarget("http://127.0.0.1:9222", async (url) => {
    requested.push(new URL(url).pathname);
    return [page];
  });
  assert.equal(selected.id, page.id);
  assert.deepEqual(requested, ["/json"]);

  const fallbackRequests = [];
  const fallback = await chooseTarget("http://127.0.0.1:9222", async (url) => {
    const path = new URL(url).pathname;
    fallbackRequests.push(path);
    return path === "/json" ? [] : [page];
  });
  assert.equal(fallback.id, page.id);
  assert.deepEqual(fallbackRequests, ["/json", "/json/list"]);
});

test("extracts matchId from the final HTTPS FIBA URL path only", () => {
  assert.equal(
    extractMatchIdFromFinalUrl("https://fibalivestats.dcd.shared.geniussports.com/u/EBF/1957531/index_et_EE.html"),
    "1957531",
  );
  assert.equal(extractMatchIdFromFinalUrl("http://fibalivestats.dcd.shared.geniussports.com/u/EBF/1957531/index_et_EE.html"), null);
  assert.equal(extractMatchIdFromFinalUrl("https://www.estlatbl.com/et/tulemused/2022212040/live-stats"), null);
  assert.equal(extractMatchIdFromFinalUrl("not a URL"), null);
});

test("builds evidence fetches guarded to the FIBA page origin", () => {
  const expression = makeFibaCaptureExpression("1957531");
  assert.match(expression, /expectedOrigin="https:\/\/fibalivestats\.dcd\.shared\.geniussports\.com"/);
  assert.match(expression, /location\.origin!==expectedOrigin/);
  assert.match(expression, /read\('\/data\/'\+encodeURIComponent\(id\)\+'\/data\.json'/);
  assert.match(expression, /read\('\/u\/EBF\/'\+encodeURIComponent\(id\)\+'\/p\.html'/);
  assert.match(expression, /credentials:'same-origin'/);
  assert.match(expression, /catch\(error\).*return \{status:null/);
  assert.throws(() => makeFibaCaptureExpression("1957531/../../other"), /digits only/);
});

test("flags challenge responses without treating them as evidence", () => {
  assert.equal(isChallengeResponse(403, ""), true);
  assert.equal(isChallengeResponse(200, "<title>Just a moment...</title>"), true);
  assert.equal(isChallengeResponse(200, "{\"tm\":{}}"), false);
});

test("accepts one 100-game run while enforcing a bounded batch limit", () => {
  assert.equal(parseBatchLimit("100"), 100);
  assert.equal(parseBatchLimit("20"), 20);
  assert.throws(() => parseBatchLimit("101"), /1 to 100/);
  assert.throws(() => parseBatchLimit("0"), /1 to 100/);
});

test("atomic JSON replacement preserves the old target and validated temp on EPERM, and checkpoint recovery can read it", async () => {
  const dir = mkdtempSync(join(tmpdir(), "arc-mapping-atomic-"));
  try {
    const target = join(dir, "evidence.json");
    writeFileSync(target, JSON.stringify({ evidence: ["old"] }));
    const rejectReplacement = (source, destination) => {
      if (destination === target && source.endsWith(".tmp")) {
        const error = new Error("simulated Windows sharing violation");
        error.code = "EPERM";
        throw error;
      }
      renameSync(source, destination);
    };
    await assert.rejects(
      atomicJson(target, { evidence: ["new"] }, { rename: rejectReplacement, retryDelays: [0, 0] }),
      /old destination was left in place.*validated staged JSON remains/,
    );
    assert.deepEqual(JSON.parse(readFileSync(target, "utf8")), { evidence: ["old"] });
    const stagedPath = join(dir, readdirSync(dir).find((name) => name.startsWith("evidence.json.") && name.endsWith(".tmp")));
    assert.deepEqual(JSON.parse(readFileSync(stagedPath, "utf8")), { evidence: ["new"] });

    const checkpoint = join(dir, "navigation-checkpoint.json");
    writeFileSync(checkpoint, JSON.stringify({ manifestPath: "manifest.json", results: { one: {} }, evidence: {} }));
    const newer = `${checkpoint}.interrupted.tmp`;
    writeFileSync(newer, JSON.stringify({ manifestPath: "manifest.json", results: { one: {}, two: {} }, evidence: { id: {} } }));
    const recovered = readBestCheckpoint(checkpoint, "manifest.json");
    assert.equal(recovered.source, newer);
    assert.equal(Object.keys(recovered.state.results).length, 2);
    assert.equal(readFileSync(newer, "utf8").length > 0, true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("captured same-origin JSON and FIBA page are checked by the unchanged ARC validator", async () => {
  const bodies = new Map([
    ["data.json", JSON.stringify({ tm: {
      1: { name: "BK Ventspils", score: 68 },
      2: { name: "Latvijas Universitate", score: 59 },
    } })],
    ["p.html", "<h6>Competition</h6><p>Paf Estonian-Latvian Basketball League</p><h6>Game Details</h6><p>Tip off: 7:00 pm GMT 5/2/22</p><p>BK Ventspils vs Latvijas Universitate</p>"],
  ]);
  const fetcher = async (url) => {
    const key = url.endsWith("data.json") ? "data.json" : "p.html";
    return new Response(bodies.get(key), {
      status: 200,
      headers: { "content-type": key === "data.json" ? "application/json" : "text/html" },
    });
  };
  const checked = await validateMapping(row, fetcher);
  assert.equal(checked.verification_status, "VERIFIED");
  assert.equal(checked.notes, row.notes);
  assert.equal(checked.source_url, row.source_url);
});
