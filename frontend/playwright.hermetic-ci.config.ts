import { defineConfig } from "@playwright/test";

import base from "./playwright.config";
import nativeCases from "./tests/fixtures/browserNativeCases.json" with { type: "json" };

const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// IMPORTANT: the driver checks exact file/title conservation before execution. A title-only
// exclusion without that check could silently remove a capable case with the same name.
export default defineConfig({
  ...base,
  grepInvert: new RegExp(
    `(?:^|\\s)(?:${nativeCases.map((row) => escape(row.title)).join("|")})$`,
  ),
  forbidOnly: true,
  workers: 1,
  retries: 0,
});
