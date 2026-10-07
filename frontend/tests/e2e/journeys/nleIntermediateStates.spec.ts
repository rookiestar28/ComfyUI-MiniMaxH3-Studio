// M25-16 Section 6 intermediate states, exercised on the real overlay component tree with
// decoded media and the real Python core behind every transaction (`canonical=1&media=1`):
//
// - an accepted receipt whose history refresh is still pending keeps the monitor on the
//   receipt's snapshot (no unmount, no second acquisition) and shows no stale selection cursor;
// - closing the view inside that window releases every owner and the late refresh reopens
//   nothing;
// - edits and a trim's pause intent issued while media is still opening coalesce onto the newest
//   owner and never cancel the in-flight acquisition.
//
// The harness's `historyDelayMs` mirrors the accepted session's two-step adoption (receipt first,
// history later); media latency is injected at the Playwright route, never in product code.
import { test, expect, type Page } from "@playwright/test";

import {
  canonicalWorkspace,
  grip,
  snapshot,
  surface,
} from "../helpers/nleCanonical";
import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";

const MONITOR = '[data-h3-nle-status="monitor"]';
const PLAY = { name: "Play", exact: true } as const;

async function routeMedia(
  page: Page,
  delayMs: { video?: number; image?: number } = {},
) {
  await routeGenericFixtureMedia(page, { delayMs });
}

const clip = (page: Page, id: string) =>
  page.locator(
    `[data-h3-nle-clip="${id}"] [data-h3-nle-control="selection.set"]`,
  );

const markCanvas = (page: Page) =>
  page.evaluate(() => {
    const canvas = document.querySelector(".h3-nle-monitor canvas");
    if (!(canvas instanceof HTMLCanvasElement)) return false;
    canvas.dataset["h3Marker"] = "before-pending";
    return true;
  });
const canvasMarker = (page: Page) =>
  page.evaluate(
    () =>
      (document.querySelector(".h3-nle-monitor canvas") as HTMLElement | null)
        ?.dataset["h3Marker"] ?? null,
  );

test("accepted receipt keeps the monitor owner and shows no stale cursor while history refresh is pending", async ({
  page,
}) => {
  await routeMedia(page);
  await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1&shape=smoke&historyDelayMs=1500",
  );
  await expect(page.getByRole("button", PLAY)).toBeEnabled();
  expect(await markCanvas(page)).toBe(true);
  const before = await snapshot(page);
  expect(before.timelineSnapshot).not.toBeNull();
  expect(before.mediaOwnership.live).toBeGreaterThan(0);

  await clip(page, "clip-0").click();
  // The pending window: receipt adopted, obsolete history dropped, refresh still in flight.
  await expect.poll(async () => (await snapshot(page)).historyPending).toBe(1);
  const pending = await snapshot(page);
  expect(pending.receipts).toBe(before.receipts + 1);
  expect(pending.timelineSnapshot).toBeNull();
  // No stale cursor: nothing is presented as selected until the refreshed history says so.
  await expect(page.locator("[data-h3-nle-selected-clip]")).toHaveCount(0);
  // The monitor stayed mounted on the receipt's snapshot: the same canvas element, no
  // release of the owner that was live before the edit beyond the one identity replacement.
  expect(await canvasMarker(page)).toBe("before-pending");
  await expect(page.locator(MONITOR)).not.toHaveText(/closed|unavailable/u);

  await expect.poll(async () => (await snapshot(page)).historyPending).toBe(0);
  await expect
    .poll(async () => (await snapshot(page)).timelineSnapshot?.timelineRevision)
    .toBe(before.timelineSnapshot!.timelineRevision + 1);
  await expect(
    page.locator('[data-h3-nle-selected-clip="clip-0"]'),
  ).toHaveCount(1);
  await expect(page.getByRole("button", PLAY)).toBeEnabled();
  const after = await snapshot(page);
  expect(after.lateHistoryAfterClose).toBe(0);
  // One replacement for the moved snapshot identity, none for the refresh that confirmed it:
  // the superseded owner's leases were released and the live set is the same size as before.
  const acquiredDelta =
    after.mediaOwnership.acquired - before.mediaOwnership.acquired;
  expect(acquiredDelta).toBeGreaterThan(0);
  expect(after.mediaOwnership.released - before.mediaOwnership.released).toBe(
    acquiredDelta,
  );
  expect(after.mediaOwnership.live).toBe(before.mediaOwnership.live);
  expect(await canvasMarker(page)).toBe("before-pending");
});

test("closing inside the pending history window releases every owner and the late refresh reopens nothing", async ({
  page,
}) => {
  await routeMedia(page);
  await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1&shape=smoke&historyDelayMs=1500",
  );
  await expect(page.getByRole("button", PLAY)).toBeEnabled();
  await clip(page, "clip-0").click();
  await expect.poll(async () => (await snapshot(page)).historyPending).toBe(1);
  const mountedBefore = (await snapshot(page)).mounted;

  await page.locator('[data-h3-nle-action="close"]').click();
  await expect(page.locator(surface)).toHaveCount(0);
  await expect
    .poll(async () => (await snapshot(page)).surfaceStatus)
    .toBe("compact_ready");
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.live)
    .toBe(0);

  // Let the delayed refresh land after the close.
  await expect.poll(async () => (await snapshot(page)).historyPending).toBe(0);
  const after = await snapshot(page);
  expect(after.lateHistoryAfterClose).toBe(1);
  expect(after.surfaceStatus).toBe("compact_ready");
  expect(after.mounted).toBe(mountedBefore);
  expect(after.closeReasons).toEqual(["explicit_close"]);
  expect(after.mediaOwnership.live).toBe(0);
  expect(after.mediaOwnership.released).toBe(after.mediaOwnership.acquired);
  await expect(page.locator(surface)).toHaveCount(0);
  // The compact authoring state adopted the refreshed history.
  expect(after.timelineSnapshot).not.toBeNull();
});

test("edits and a trim pause intent during delayed media opening coalesce onto the newest owner without cancelling acquisition", async ({
  page,
}) => {
  await routeMedia(page, { video: 2500 });
  await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1&shape=smoke&rebindDelayMs=1000",
  );
  await expect(page.locator(MONITOR)).toHaveText("Opening monitor.");
  const opening = await snapshot(page);

  // Two accepted edits while the first owner is still opening: a selection, then a keyboard
  // trim whose draft start issues the monitor a pause intent.
  await clip(page, "clip-0").click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await expect(page.locator(MONITOR)).toHaveText("Opening monitor.");
  await page.locator(grip).first().focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const commands = (await snapshot(page)).intents.map(
    (intent) => intent.commands[0]?.kind,
  );
  expect(commands).toEqual(["select_clips", "trim_clip"]);

  // The owner settles paused, never blocked: the pause was retained, not sent to an opening
  // owner, and the queued replacements collapsed onto the newest binding.
  await expect(page.locator(MONITOR)).toHaveText("Monitor paused.", {
    timeout: 20_000,
  });
  await expect(page.getByRole("button", PLAY)).toBeEnabled();
  const settled = await snapshot(page);
  expect(settled.timelineSnapshot!.timelineRevision).toBe(
    opening.timelineSnapshot!.timelineRevision + 2,
  );
  expect(settled.mediaOwnership.released).toBe(
    settled.mediaOwnership.acquired - settled.mediaOwnership.live,
  );

  // Initial acquisition settles once, then exactly one reauthorization adopts the newest queued
  // revision. A later in-place edit cannot calibrate the old full-replacement owner count.
  const perOwner = settled.mediaOwnership.sourceLive;
  expect(perOwner).toBeGreaterThan(0);
  expect([
    ...new Set(settled.mediaOwnership.sourceAcquireTimelineRevisions),
  ]).toEqual([opening.timelineSnapshot!.timelineRevision]);
  expect([
    ...new Set(settled.mediaOwnership.sourceRebindTimelineRevisions),
  ]).toEqual([settled.timelineSnapshot!.timelineRevision]);
  expect(settled.mediaOwnership.sourceRebinds).toBe(
    settled.mediaOwnership.videoElements,
  );
  // A real property edit against the settled owner reauthorizes without another open. Observe
  // its pending authority directly; the kept picture and paused status must stay available.
  await page.locator(grip).first().focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(3);
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.rebindsInFlight)
    .toBe(1);
  await expect(page.locator(MONITOR)).toHaveText("Monitor paused.");
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.sourceRebinds)
    .toBe(
      settled.mediaOwnership.sourceRebinds +
        settled.mediaOwnership.videoElements,
    );
  await expect(page.locator(MONITOR)).toHaveText("Monitor paused.", {
    timeout: 20_000,
  });
  const calibrated = await snapshot(page);
  expect(calibrated.mediaOwnership.leaseOpens).toBe(
    settled.mediaOwnership.leaseOpens,
  );
  expect(calibrated.mediaOwnership.videoElements).toBe(
    settled.mediaOwnership.videoElements,
  );
  expect(calibrated.mediaOwnership.sourceLive).toBe(
    settled.mediaOwnership.sourceLive,
  );
  expect(calibrated.mediaOwnership.maximumSourceLive).toBeLessThanOrEqual(
    2 * perOwner,
  );
  // No decoration outlives its publish: every live owner is a monitor source owner.
  expect(calibrated.mediaOwnership.live).toBe(
    calibrated.mediaOwnership.sourceLive,
  );
});

// B-M2563-14: the lease scheduler preempts an in-flight decoration whenever the monitor starts a
// playback acquisition (`nleLeaseScheduler` "playback_priority"), by design. The harness fetched a
// decoration's image with its own signal, so a preempted request still completed, decoded and
// minted an owner; `acquired` then counted every preemption. Images are delayed here so every
// preemption lands while the fetch is in flight.
test("a decoration the scheduler preempts cancels its fetch and mints no owner", async ({
  page,
}) => {
  const cancelledImages: string[] = [];
  page.on("requestfailed", (request) => {
    if (new URL(request.url()).pathname === "/nle-media/image")
      cancelledImages.push(request.url());
  });
  await routeMedia(page, { video: 2500, image: 400 });
  await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1&shape=smoke&h3NleSchedulerTrace=1",
  );
  await expect(page.locator(MONITOR)).toHaveText("Monitor paused.", {
    timeout: 30_000,
  });
  const events = (name: string) =>
    page.evaluate(
      (event) =>
        (
          (
            window as Window & {
              __h3ContextNleSchedulerTrace?: { event: string }[];
            }
          ).__h3ContextNleSchedulerTrace ?? []
        ).filter((entry) => entry.event === event).length,
      name,
    );
  expect(await events("decoration.abort")).toBeGreaterThan(0);
  // The preempted fetch was cancelled, not left to complete.
  expect(cancelledImages.length).toBeGreaterThan(0);
  // Every decoration owner is one the scheduler received.
  await expect
    .poll(async () => {
      const owners = (await snapshot(page)).mediaOwnership;
      return (
        owners.acquired -
        owners.sourceAcquired -
        (await events("decoration.acquire.succeeded"))
      );
    })
    .toBe(0);
  const owners = (await snapshot(page)).mediaOwnership;
  expect(owners.released).toBe(owners.acquired - owners.live);
});
