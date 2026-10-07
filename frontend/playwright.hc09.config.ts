import { defineConfig } from "@playwright/test";

export function resolveHc09PlaywrightOutput(
  environment: Readonly<{ H3_CONTEXT_PLAYWRIGHT_OUTPUT?: string }>,
): string {
  return environment.H3_CONTEXT_PLAYWRIGHT_OUTPUT ?? "test-results/hc09";
}

export default defineConfig({
  testDir: "tests/e2e",
  testMatch: "journeys/hostSeams/hc09HostSeamCapture.spec.ts",
  outputDir: resolveHc09PlaywrightOutput(process.env),
  fullyParallel: false,
  retries: 0,
  workers: 1,
  preserveOutput: "never",
  timeout: 60_000,
  expect: { timeout: 30_000 },
  use: {
    headless: true,
    viewport: { width: 1280, height: 900 },
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  reporter: [["line"]],
});
