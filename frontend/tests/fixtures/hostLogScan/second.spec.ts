import { appendFileSync } from "node:fs";

import { expect, test } from "../../e2e/host/fixture";
import { candidateInjectionStates } from "../../e2e/host/candidate";

const log = process.env.H3_CONTEXT_HOST_LOG!;
const foreign = [
  "Error handling request from 127.0.0.1",
  "Traceback (most recent call last):",
  '  File "X:\\HostRoot\\custom_nodes\\other-pack\\server.py", line 1, in handle',
  "",
].join("\n");
const owned = [
  "Error handling request from 127.0.0.1",
  "Traceback (most recent call last):",
  '  File "X:\\HostRoot\\custom_nodes\\comfyui_h3_context\\adapters\\routes.py", line 1, in handle',
  "RuntimeError: synthetic cleanup refusal",
  "",
].join("\n");

test.beforeEach(async ({ context }) => {
  expect(candidateInjectionStates.has(context)).toBe(true);
});

test.afterEach(async ({}, testInfo) => {
  if (testInfo.title === "cleanup and owned log failures stay separate") {
    appendFileSync(log, owned, "utf8");
    throw new Error("synthetic cleanup failure");
  }
});

test("second file clean row", async () => {
  appendFileSync(log, foreign, "utf8");
  expect(true).toBe(true);
});

test("cleanup and owned log failures stay separate", async () => {
  expect(true).toBe(true);
});

test.skip("a skipped row creates no fake receipt", async () => {
  throw new Error("a statically skipped body cannot execute");
});
