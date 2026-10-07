// M25-21 hardening qualification lane. It runs every hardening journey, including the long
// NLE-STRESS-V1, UI-CONTRACT-STRESS-V1 and output workloads that stay out of the ordinary
// hermetic lane (`playwright.config.ts`) because they take tens of minutes. Same server, port
// policy, single worker and no retries: a retry would turn a flaky budget into a pass.
import { defineConfig } from "@playwright/test";

import base from "./playwright.config";

export default defineConfig({
  ...base,
  testMatch: [
    "journeys/nleHardeningCommands.spec.ts",
    "journeys/nleHardeningPresentationClock.spec.ts",
    "journeys/nleHardeningShell.spec.ts",
    "journeys/nleHardeningActions.spec.ts",
    "journeys/nleHardeningStress.spec.ts",
    "journeys/nleHardeningRender.spec.ts",
  ],
  outputDir:
    process.env.H3_CONTEXT_PLAYWRIGHT_OUTPUT ?? "test-results/hardening",
  use: {
    ...base.use,
    // IMPORTANT (M25-44 B-M2544-08): the stress workloads set test budgets of up to 40 minutes,
    // and an action without its own limit inherits that budget. A locator that can never become
    // actionable (a clip button on a timeline row that is not mounted) then hangs the whole
    // workload instead of failing that step in seconds with the locator named.
    actionTimeout: 30_000,
  },
});
