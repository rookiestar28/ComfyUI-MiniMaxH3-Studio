import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests/fixtures/hostLogScan",
  testMatch: "*.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  preserveOutput: "never",
  use: {
    headless: true,
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  reporter: [["json"]],
});
