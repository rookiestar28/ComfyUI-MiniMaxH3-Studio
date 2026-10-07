// A catalog video asset is prepared for playback before any clip uses it. This journey runs the
// browser half of that on the real overlay component tree: the lease scheduler, the demand
// broker and the preparation hook are the product's own. The lease client is the hermetic
// harness client, whose preparation takes `preparationDelayMs`, so that playback arrives while
// one is running; the scheduler's own trace (`h3NleSchedulerTrace=1`) says what it did and when.
import { expect, test, type Page } from "@playwright/test";

import type { NleWorkspaceHarnessSnapshot } from "../../../e2e/nleWorkspace";
import type { NleLeaseSchedulerEvent } from "../../../src/host/nleLeaseScheduler";
import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";

const MONITOR = '[data-h3-nle-status="monitor"]';
const PREPARATION = "playback_preparation";
const DELAY_MS = 2_000;
// The V2 fixture's catalog: two video assets with bound audio, one timing row per frame.
const CATALOG = [
  "generated.asset.1:video_proxy",
  "generated.asset.1:audio_preview",
  "generated.asset.2:video_proxy",
  "generated.asset.2:audio_preview",
];

const snapshot = (page: Page): Promise<NleWorkspaceHarnessSnapshot> =>
  page.evaluate(() => window.nleWorkspaceHarness.snapshot());

const schedulerTrace = (page: Page): Promise<NleLeaseSchedulerEvent[]> =>
  page.evaluate(
    () =>
      (
        window as Window & {
          __h3ContextNleSchedulerTrace?: NleLeaseSchedulerEvent[];
        }
      ).__h3ContextNleSchedulerTrace ?? [],
  );

const decorationsPending = (event: NleLeaseSchedulerEvent) =>
  event.pendingKinds.filter((kind) => kind !== PREPARATION);

test("playback takes the scheduler from a running preparation, and the catalog is prepared behind every decoration", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await page.goto(
    `/nleWorkspace.html?authoringV2=1&media=1&frames=72&preparationDelayMs=${DELAY_MS}&h3NleSchedulerTrace=1`,
  );
  await page.getByRole("button", { name: "Open full editor" }).click();

  // No clip uses either asset, and one of them is being prepared all the same.
  await expect
    .poll(async () => (await snapshot(page)).mediaPreparation.inFlight)
    .toBe(1);
  const opened = await snapshot(page);
  expect(opened.timelineSnapshot).toBeNull();
  expect(opened.mediaPreparation.aborted).toBe(0);
  // The bin's two thumbnails were acquired before the first preparation was started.
  const atOpen = await schedulerTrace(page);
  const firstPreparation = atOpen.findIndex(
    (event) =>
      event.event === "decoration.acquire.start" &&
      event.activeKind === PREPARATION,
  );
  expect(firstPreparation).toBeGreaterThan(0);
  expect(
    atOpen
      .slice(0, firstPreparation)
      .filter(
        (event) =>
          event.event === "decoration.acquire.succeeded" &&
          event.activeKind === "thumbnail",
      ),
  ).toHaveLength(2);

  // Placing a clip makes the monitor acquire its source while that preparation is running.
  await page
    .getByRole("button", { name: "Add Clip 01 to the timeline" })
    .click();
  await expect(page.locator(MONITOR)).toHaveText("Monitor paused.", {
    timeout: 20_000,
  });
  const playing = await snapshot(page);
  expect(playing.mediaPreparation.aborted).toBeGreaterThan(0);
  expect(playing.mediaOwnership.sourceLive).toBeGreaterThan(0);

  // The scheduler aborted the preparation for playback, joined its release, and only then let
  // playback acquire; nothing was started in between.
  const afterInsert = await schedulerTrace(page);
  const abort = afterInsert.findIndex(
    (event) =>
      event.event === "decoration.abort" &&
      event.reason === "playback_priority" &&
      event.activeKind === PREPARATION,
  );
  expect(abort).toBeGreaterThan(firstPreparation);
  const next = (name: string) =>
    afterInsert.findIndex(
      (event, index) => index > abort && event.event === name,
    );
  const settled = next("decoration.settled");
  const acquired = next("playback.acquire.succeeded");
  expect(settled).toBeGreaterThan(abort);
  expect(acquired).toBeGreaterThan(settled);
  expect(
    afterInsert
      .slice(abort, acquired)
      .filter((event) => event.event === "decoration.acquire.start"),
  ).toEqual([]);

  // The catalog is prepared all the same: each asset and kind once, in catalog order, every
  // lease released, and the aborted request is the only one that was made in vain.
  await expect
    .poll(async () => (await snapshot(page)).mediaPreparation.prepared, {
      timeout: 30_000,
    })
    .toEqual(CATALOG);
  await expect
    .poll(async () => (await snapshot(page)).mediaPreparation.inFlight)
    .toBe(0);
  const prepared = (await snapshot(page)).mediaPreparation;
  expect(prepared.released).toBe(CATALOG.length);
  expect(prepared.attempts).toBe(CATALOG.length + prepared.aborted);

  // Every request the harness client received was started while no decoration was waiting: a
  // preparation the scheduler put back at the head of its queue gave way without asking.
  const trace = await schedulerTrace(page);
  const starts = trace.filter(
    (event) =>
      event.event === "decoration.acquire.start" &&
      event.activeKind === PREPARATION,
  );
  expect(prepared.attemptsAtMs).toHaveLength(prepared.attempts);
  for (const at of prepared.attemptsAtMs) {
    const start = starts.filter((event) => event.monotonicMs <= at).at(-1);
    expect(
      start,
      `no scheduler start precedes the request at ${at}`,
    ).toBeDefined();
    expect(decorationsPending(start!)).toEqual([]);
  }
  // The clip's own decorations were acquired before the preparation that playback had aborted
  // was asked for again.
  const retried = starts
    .filter((event) => event.monotonicMs <= prepared.attemptsAtMs[1]!)
    .at(-1)!;
  expect(
    trace.filter(
      (event) =>
        event.sequence > afterInsert[abort]!.sequence &&
        event.sequence < retried.sequence &&
        event.event === "decoration.acquire.succeeded" &&
        event.activeKind !== PREPARATION,
    ).length,
  ).toBeGreaterThan(0);

  // Preparation holds nothing in the page: every owner is the monitor's source or a decoration
  // the scheduler received, and whatever is not live has been released.
  await expect
    .poll(async () => {
      const owners = (await snapshot(page)).mediaOwnership;
      const decorations = (await schedulerTrace(page)).filter(
        (event) =>
          event.event === "decoration.acquire.succeeded" &&
          event.activeKind !== PREPARATION,
      ).length;
      return owners.acquired - owners.sourceAcquired - decorations;
    })
    .toBe(0);
  const owners = (await snapshot(page)).mediaOwnership;
  expect(owners.released).toBe(owners.acquired - owners.live);
  expect(owners.live).toBe(owners.sourceLive);
});
