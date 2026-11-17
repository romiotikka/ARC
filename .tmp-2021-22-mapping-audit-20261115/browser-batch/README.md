# ARC 2021–22 browser navigation recovery

This workflow collects candidates and raw evidence only. It does not edit the mapping CSV, apply mappings, access `data/arc2.db`, or run an import.

## Browser execution flow

The batch runner is a local Node process attached to Chrome through a **localhost-only** Chrome DevTools Protocol endpoint. The Node process, rather than a page script, is the supervisor that survives navigation. For each EstLatBL game it:

1. Navigates the existing Chrome tab to the official EstLatBL `/live-stats` URL. Chrome follows the page's redirect normally to the new HTTPS FIBA LiveStats domain.
2. Reads `location.href` in the final page's own execution context and extracts the ID from `/u/EBF/{matchId}/…`.
3. While that tab is on the FIBA origin, evaluates relative fetches for `/data/{matchId}/data.json` and `/u/EBF/{matchId}/p.html`. Both requests run in the FIBA page context, so this does not depend on cross-origin access from EstLatBL.
4. Writes the local checkpoint atomically after every game. At the end of the run, it builds the merge-ready candidate/evidence bundles from that checkpoint. If Node or Chrome is interrupted, rerunning the same command resumes from the checkpoint and skips previously attempted games. Use `--retry PROVIDER_GAME_ID` only for a game that should be retried; its earlier attempt is retained in the checkpoint.

The collector processes at most 100 games per run. Extracted match IDs remain `CANDIDATE_EXTRACTED_UNVERIFIED`. The existing 61 VERIFIED mappings and the separate `2022212147 → 1999572` UNVERIFIED candidate are copied from the manifest and never overwritten by this runner. Duplicate IDs are marked for review. Individual FIBA evidence HTTP failures are recorded and the batch continues. A Cloudflare/security response, failed browser navigation, or lost page context is checkpointed and stops the batch; rerunning the same command retries that stopped game first, then continues with untouched games.

## Chrome requirement

Chrome must be running with its DevTools Protocol endpoint bound to localhost, normally `http://127.0.0.1:9222`. Discovery tries `/json` first and falls back to `/json/list`, then requires a page target with an HTTPS EstLatBL URL and a `webSocketDebuggerUrl`; it does not filter by title. The runner only attaches to an already open EstLatBL tab; it does not launch Chrome, change Chrome settings, solve challenges, or expose the endpoint on the network. It fails before page navigation if the endpoint is unavailable.

The current Chrome session was checked during implementation: no endpoint was available on port 9222. Therefore the runner is implemented, but it has **not** been run against the 132 games. Do not expose a debugging port on a public interface. If Chrome must be relaunched to enable local debugging, first ensure the verified browser session can be used in that launched instance; Chrome may require a non-default user-data directory for remote debugging.

## Run one batch

From `C:\Users\Kasutaja\Desktop\ARC`, with the intended Chrome session already open on an EstLatBL page, run:

```powershell
node .tmp-2021-22-mapping-audit-20261115/browser-batch/navigate-and-capture.mjs --cdp http://127.0.0.1:9222 --limit 100
```

To resume after a safe pause, run the same command after ordinary browser access is restored. To retry a previously attempted game after reviewing its error:

```powershell
node .tmp-2021-22-mapping-audit-20261115/browser-batch/navigate-and-capture.mjs --cdp http://127.0.0.1:9222 --limit 100 --retry 2022212040
```

The runner creates or updates:

- `navigation-checkpoint.json` — per-game attempts, final URLs, extracted IDs and raw evidence.
- `arc-2021-22-live-stats-candidates.json` — candidate export compatible with the existing offline validator.
- `arc-2021-22-fiba-evidence.json` — FIBA JSON and `p.html` responses captured from the FIBA origin.

If a bundle could not be replaced because Windows temporarily locked the destination, the validated temporary JSON and checkpoint are retained. After the lock is gone, rebuild both bundles from the checkpoint without opening Chrome or navigating by running:

```powershell
node .tmp-2021-22-mapping-audit-20261115/browser-batch/navigate-and-capture.mjs --export-only
```

If a page shows a Cloudflare challenge or security response, the runner records it and leaves the mapping unverified. Solve any challenge normally in Chrome; then use `--retry` for that game. Do not automate or bypass challenges.

## Validate captured evidence offline

From the repository root, pass the generated bundles to ARC's existing offline bridge. This writes only review files in the isolated audit folder:

```powershell
node .tmp-2021-22-mapping-audit-20261115/browser-batch/validate-browser-evidence.mjs `
  --csv data/estlatbl_2021_22_mapping_overrides.csv `
  --candidates .tmp-2021-22-mapping-audit-20261115/browser-batch/arc-2021-22-live-stats-candidates.json `
  --evidence .tmp-2021-22-mapping-audit-20261115/browser-batch/arc-2021-22-fiba-evidence.json `
  --out .tmp-2021-22-mapping-audit-20261115/browser-batch/candidates-validated.csv `
  --report .tmp-2021-22-mapping-audit-20261115/browser-batch/candidates-validation.json
```

The bridge calls the existing `validateMapping()` implementation with captured responses; it requires the existing team, score, date, competition and page-team checks. HTTP 200 alone never verifies a mapping. Review the generated CSV/report. Do not run the separate `mapping_recovery.mjs apply` command for this workflow.

## Legacy console scripts

`collect-live-stats.js` and `capture-fiba-evidence.js` are retained as earlier manual console tools. They are not the batch entry point: the former fetches the EstLatBL URL in page context and cannot reliably observe a final cross-origin URL after navigation; the latter is useful only when already operating on the FIBA origin. Use `navigate-and-capture.mjs` for the supervised batch flow.

## Verification

- `navigate-and-capture.mjs` and the two legacy browser scripts pass `node --check`.
- Focused mapping/browser tests cover final-URL extraction, FIBA-origin fetch construction, challenge classification, and existing offline evidence validation.
- The Chrome DevTools endpoint was unavailable locally during implementation, so no live game page or FIBA response was requested by the new runner.
