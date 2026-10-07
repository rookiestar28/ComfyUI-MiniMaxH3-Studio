import { defineConfig } from "@playwright/test";

// M25-20 F1 corrective: the 151 `render_and_browser` rows take several minutes to present in
// turn, so this spec is deliberately not part of `playwright.config.ts`'s `test:e2e:ci` testMatch
// (`tests/TEST_SOP.md` section 3.6 places the semantic-conformance qualification run's per-row
// browser evidence outside the ordinary Full Gate for exactly that resource reason). This config
// mirrors `playwright.config.ts`'s hermetic settings, scoped to just that one spec, so it can be
// run directly or through `nleSemanticBrowserStageGatherer.mjs` without lengthening every ordinary
// CI run.

const e2ePortText = process.env.H3_CONTEXT_E2E_PORT ?? "4173";
if (!/^[1-9]\d{0,4}$/.test(e2ePortText))
  throw new Error("H3_CONTEXT_E2E_PORT must be a decimal TCP port");
const e2ePort = Number(e2ePortText);
if (e2ePort > 65535)
  throw new Error("H3_CONTEXT_E2E_PORT must be at most 65535");
const e2eBaseUrl = `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  testDir: "tests/e2e",
  testMatch: ["journeys/nleSemanticRenderAndBrowser.spec.ts"],
  outputDir:
    process.env.H3_CONTEXT_PLAYWRIGHT_OUTPUT ??
    "test-results-render-and-browser",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  timeout: 1_800_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: e2eBaseUrl,
    headless: true,
    viewport: { width: 1280, height: 900 },
    trace: "off",
    screenshot: "off",
    video: "off",
    // GUARD: this spec's single `test()` presents all 151 rows in one page, each wrapped in its
    // own try/catch so one row's failure cannot lose another row's evidence -- but that guard only
    // works if the *action that fails* actually fails in bounded time. A raw `.fill()`/`.click()`
    // with no `actionTimeout` set inherits the whole per-test budget (`timeout` above) as its own
    // retry ceiling, so a locator that becomes permanently disabled (observed directly: the
    // transport.seek input goes `disabled`, `aria-valuetext="Frame unavailable"`, on
    // `embedded_audio.primary_without_audio`) hangs the single action for the whole test budget and
    // takes every row after it down with it, rather than failing that one row in seconds.
    //
    // This bounds the *responsive-but-not-actionable* case (a control that stays disabled/hidden).
    // It does not bound a page whose own main thread is genuinely wedged: Playwright's actionability
    // polling round-trips into the page to ask "is this element ready", and when the page cannot
    // answer, that polling never gets a response for this timeout to fire against either (observed
    // directly: `effect.contrast_permille.lower` sat inside one `locator.fill()` for the entire test
    // budget with no retry log at all, unlike the merely-disabled case above, which retries quickly
    // and visibly). `nleSemanticRenderAndBrowser.spec.ts`'s own `withTimeout` -- a plain Node timer
    // race that never itself needs the page to respond -- is the real backstop for that case, plus a
    // bounded health probe that discards a wedged page for a fresh one rather than retrying forever.
    actionTimeout: 15_000,
  },
  reporter: [["line"]],
  webServer: {
    command: `pnpm exec vite --config vite.e2e.config.ts --host 127.0.0.1 --port ${e2ePort}`,
    url: e2eBaseUrl,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
