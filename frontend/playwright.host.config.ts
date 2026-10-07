import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  testMatch: "journeys/host/*.spec.ts",
  outputDir: process.env.H3_CONTEXT_PLAYWRIGHT_OUTPUT ?? "test-results-host",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  // A supplied host can carry unrelated private canvas content. Never retain
  // Playwright failure snapshots or attachments from this compatibility lane.
  preserveOutput: "never",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    // A supplied host can leave a control disabled for reasons this lane has to
    // report rather than wait out: without a bound, an action on a control that
    // never becomes ready hangs until the test timeout and says nothing about
    // which control it was.
    actionTimeout: 60_000,
    // IMPORTANT: headed supplied-host evidence is opt-in through the host runner's closed row
    // controls. Do not read a generic browser flag here; inherited H3 controls are stripped so
    // an unrelated shell cannot silently change the qualification venue.
    headless: process.env.H3_CONTEXT_HOST_HEADED !== "1",
    viewport: { width: 1280, height: 900 },
    // M22-16's explicitly enabled read-only composition must intercept the provider projection
    // before it reaches an older installed backend. Service-worker owned requests bypass
    // Playwright routing unless this one bounded lane blocks them.
    serviceWorkers:
      process.env.H3_CONTEXT_M22_16_READ_ONLY_COMPOSITION === "1"
        ? "block"
        : "allow",
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  reporter: [["line"]],
});
