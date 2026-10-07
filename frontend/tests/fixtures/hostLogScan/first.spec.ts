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

test.beforeEach(async ({ context }, testInfo) => {
  expect(candidateInjectionStates.has(context)).toBe(true);
  if (testInfo.title === "setup failure is still scanned") {
    appendFileSync(log, foreign, "utf8");
    throw new Error("synthetic setup failure");
  }
});

test("first file clean row", async () => {
  appendFileSync(log, foreign, "utf8");
  expect(true).toBe(true);
});

test("setup failure is still scanned", async () => {
  throw new Error("setup should have stopped the body");
});

test("body failure is still scanned", async () => {
  appendFileSync(log, foreign, "utf8");
  expect("body").toBe("failure preserved");
});
