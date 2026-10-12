import config from "./playwright.config";
import { defineConfig } from "@playwright/test";

export default defineConfig({
  ...config,
  testMatch: ["journeys/titlePixelParity.spec.ts"],
});
