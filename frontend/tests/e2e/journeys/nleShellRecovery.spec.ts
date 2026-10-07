// M25-16 corrective F1 at the REAL integrated shell: a timeline transaction whose reply is lost
// after the core committed it must surface as an unknown outcome (never a rejection), be
// reconciled by exactly one read-only history read with nothing replayed, show the committed
// edit, and let the next trim proceed on the refreshed base as a separate transaction.
import { test, expect } from "@playwright/test";

import { grip } from "../helpers/nleCanonical";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

const clipZero = async (page: Parameters<typeof shellSnapshot>[0]) =>
  (await shellSnapshot(page)).timelineSnapshot!.clips.find(
    (clip) => clip.clipId === "clip-0",
  )!;

test("a transport-lost trim is reconciled read-only as unknown, never rejected or replayed", async ({
  page,
}) => {
  // Transaction 0 is the selection; transaction 1 (the trim) is committed by the oracle and its
  // reply is then dropped.
  const oracle = await openIntegratedShell(page, "smoke", {
    reply: (index) => (index === 1 ? "abort" : "fulfill"),
  });
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  const before = await clipZero(page);
  const readsBefore = oracle.reads();

  const handle = page.locator(grip).filter({ visible: true }).first();
  await handle.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");

  // The session ends in `ready` after its single reconciliation read, showing the committed
  // edit although no receipt for it was ever received.
  await expect
    .poll(async () => (await shellSnapshot(page)).authoringStatus)
    .toBe("ready");
  const after = await clipZero(page);
  expect(after.durationFrames).toBe(before.durationFrames - 2);
  expect(after.startFrame).toBe(before.startFrame);
  const snapshot = await shellSnapshot(page);
  expect(snapshot.receipts).toBe(1);
  expect(oracle.transactions).toHaveLength(2);
  expect(oracle.reads() - readsBefore).toBe(1);
  // The intermediate state was rendered as unknown with the gesture reconciling: the trace
  // never says "rejected" for the lost reply.
  const trace = snapshot.commitStates;
  expect(
    trace.some(
      (state) =>
        state.includes("outcome is unknown") && state.endsWith(":reconciling"),
    ),
  ).toBe(true);
  expect(trace.some((state) => /rejected/i.test(state))).toBe(false);
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "idle",
  );

  // A fresh trim proceeds on the refreshed base as a separate, receipted transaction.
  await handle.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  expect(oracle.transactions).toHaveLength(3);
  const rebased = oracle.transactions[2] as Record<string, unknown>;
  expect(rebased.expected_timeline_revision).toBe(
    snapshot.timelineSnapshot!.timelineRevision,
  );
  expect((await clipZero(page)).durationFrames).toBe(before.durationFrames - 3);
});

test("a trim whose reply body cannot be read is reconciled read-only as unknown, never rejected", async ({
  page,
}) => {
  // Post-corrective review 02, R2-F1: the residual half of the lost-reply finding. The status
  // line arrives for a transaction the core has already committed and the body is then
  // unreadable, so no receipt can be decoded. Before the fix the client's body consumption sat
  // outside its transport catch, the session classified this as `internal_failure` against
  // pre-submit history, and the UI reported a rejection for an edit the backend had applied.
  const oracle = await openIntegratedShell(page, "smoke", {
    reply: (index) => (index === 1 ? "truncate" : "fulfill"),
  });
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  const before = await clipZero(page);
  const readsBefore = oracle.reads();

  const handle = page.locator(grip).filter({ visible: true }).first();
  await handle.focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");

  await expect
    .poll(async () => (await shellSnapshot(page)).authoringStatus)
    .toBe("ready");
  const after = await clipZero(page);
  expect(after.durationFrames).toBe(before.durationFrames - 2);
  expect(after.startFrame).toBe(before.startFrame);
  const snapshot = await shellSnapshot(page);
  // One write, one bounded read-only reconciliation, no receipt claimed and nothing replayed.
  expect(snapshot.receipts).toBe(1);
  expect(oracle.transactions).toHaveLength(2);
  expect(oracle.reads() - readsBefore).toBe(1);
  const trace = snapshot.commitStates;
  expect(
    trace.some(
      (state) =>
        state.includes("outcome is unknown") && state.endsWith(":reconciling"),
    ),
  ).toBe(true);
  // While the read was in flight the status said it was being re-read, never that it had been.
  expect(trace.some((state) => state.includes("re-reading"))).toBe(true);
  expect(trace.some((state) => state.includes("was re-read"))).toBe(false);
  expect(trace.some((state) => /rejected/i.test(state))).toBe(false);
  await expect(page.locator('[data-h3-nle-status="trim"]')).toHaveAttribute(
    "data-h3-nle-trim-phase",
    "idle",
  );
});
