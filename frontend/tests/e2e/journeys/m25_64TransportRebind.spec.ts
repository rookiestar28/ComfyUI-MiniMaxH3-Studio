// M25-64 A64-8 (B-M2561-04): an accepted edit rebinds the monitor to the new revision -- the lease
// authority admits a lease only for the current history, so a selection is a new revision too --
// and the transport must not notice. The playhead keeps its frame (clamped to the new duration),
// the ruler never turns `aria-disabled`, and a ruler press straight after an edit's receipt lands.

import { expect, test, type Locator, type Page } from "@playwright/test";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot } from "../helpers/nleCanonical";
import { openIntegratedShell, shellSnapshot } from "../helpers/nleShell";
import {
  playheadFrame,
  playheadSlider,
  pressRuler,
  seekPlayhead,
} from "../helpers/nleTimeline";

const CANVAS = '[data-h3-nle-canvas="composition"]';

test("a property edit reauthorizes one audio-bearing clip while keeping its pixels and decoded resources", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(
    page,
    (wire) => {
      const clips = wire.clips as Record<string, unknown>[];
      const selected = clips.find((clip) => clip.clip_id === "clip-0")!;
      wire.clips = [selected];
      wire.assets = (wire.assets as Record<string, unknown>[]).filter(
        (asset) => asset.asset_id === selected.asset_id,
      );
      wire.tracks = (wire.tracks as Record<string, unknown>[]).filter(
        (track) => track.track_id === selected.track_id,
      );
      (wire.output as Record<string, unknown>).duration_frames =
        Number(selected.start_frame) + Number(selected.duration_frames);
    },
    undefined,
    "&media=1&shape=smoke&rebindDelayMs=500",
  );
  const monitor = page.locator('[data-h3-nle-status="monitor"]');
  await expect(monitor).toHaveText("Monitor paused.");
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.rebindsInFlight)
    .toBe(0);
  const ruler = playheadSlider(page);
  await seekPlayhead(page, ruler, 12);
  await presented(page, 12);
  await expect
    .poll(
      async () => (await snapshot(page)).mediaOwnership.acquisitionsInFlight,
    )
    .toBe(0);
  const before = await snapshot(page);
  expect(before.timelineSnapshot!.assets[0]!.embeddedAudio).toBe(
    "present_bound",
  );
  expect(before.mediaOwnership.videoElements).toBe(1);
  await page.evaluate(() => {
    const canvas = document.querySelector(
      '[data-h3-nle-canvas="composition"]',
    ) as HTMLCanvasElement;
    const painter = canvas.getContext("2d")!;
    const status = document.querySelector('[data-h3-nle-status="monitor"]')!;
    const states: string[] = [];
    const samples: {
      sameCanvas: boolean;
      alpha: number;
      frame: string | null;
    }[] = [];
    let videos = 0;
    let decoded = 0;
    let running = true;
    let raf = 0;
    const nativeCreate = document.createElement.bind(document);
    document.createElement = ((
      name: string,
      options?: ElementCreationOptions,
    ) => {
      if (name.toLowerCase() === "video") videos++;
      return nativeCreate(name, options);
    }) as typeof document.createElement;
    const nativeBuffer = AudioContext.prototype.createBuffer;
    AudioContext.prototype.createBuffer = function (...args) {
      decoded++;
      return nativeBuffer.apply(this, args);
    };
    const observer = new MutationObserver(() =>
      states.push(status.textContent ?? ""),
    );
    observer.observe(status, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    const sample = () => {
      samples.push({
        sameCanvas:
          document.querySelector('[data-h3-nle-canvas="composition"]') ===
          canvas,
        alpha: painter.getImageData(
          Math.floor(canvas.width / 2),
          Math.floor(canvas.height / 2),
          1,
          1,
        ).data[3]!,
        frame: canvas.getAttribute("data-h3-nle-presented-frame"),
      });
      if (running) raf = requestAnimationFrame(sample);
    };
    sample();
    (window as unknown as { stopRebindProbe(): unknown }).stopRebindProbe =
      () => {
        running = false;
        cancelAnimationFrame(raf);
        observer.disconnect();
        document.createElement = nativeCreate;
        AudioContext.prototype.createBuffer = nativeBuffer;
        return { videos, decoded, states, samples };
      };
  });
  const opacity = page.getByRole("spinbutton", {
    name: "Opacity (%)",
    exact: true,
  });
  await opacity.fill("80");
  await opacity.press("Enter");
  await expect
    .poll(async () => (await snapshot(page)).receipts)
    .toBe(before.receipts + 1);
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.sourceRebinds)
    .toBe(before.mediaOwnership.sourceRebinds + 1);
  await presented(page, 12);
  const after = await snapshot(page);
  const probe = await page.evaluate(() =>
    (
      window as unknown as {
        stopRebindProbe(): {
          videos: number;
          decoded: number;
          states: string[];
          samples: {
            sameCanvas: boolean;
            alpha: number;
            frame: string | null;
          }[];
        };
      }
    ).stopRebindProbe(),
  );
  expect(after.timelineSnapshot!.timelineRevision).toBe(
    before.timelineSnapshot!.timelineRevision + 1,
  );
  expect(
    after.mediaOwnership.leaseCreates - before.mediaOwnership.leaseCreates,
  ).toBe(2);
  expect(after.mediaOwnership.leaseOpens).toBe(
    before.mediaOwnership.leaseOpens,
  );
  expect(after.mediaOwnership.sourceLive).toBe(
    before.mediaOwnership.sourceLive,
  );
  expect(probe.videos).toBe(0);
  expect(probe.decoded).toBe(0);
  expect(
    probe.states.filter((value) => /opening|closing|unavailable/i.test(value)),
  ).toEqual([]);
  expect(probe.samples.length).toBeGreaterThan(2);
  expect(
    probe.samples.every(
      (sample) =>
        sample.sameCanvas && sample.alpha === 255 && sample.frame === "12",
    ),
  ).toBe(true);
});

/** Every `aria-disabled` and `aria-valuenow` the ruler shows from now on. */
async function watchRuler(ruler: Locator) {
  await ruler.evaluate((element) => {
    const seen: string[] = [];
    (window as unknown as { __rulerSeen: string[] }).__rulerSeen = seen;
    new MutationObserver(() =>
      seen.push(
        `${element.getAttribute("aria-disabled")}:${element.getAttribute("aria-valuenow")}`,
      ),
    ).observe(element, {
      attributes: true,
      attributeFilter: ["aria-disabled", "aria-valuenow"],
    });
  });
}

const rulerSeen = (page: Page) =>
  page.evaluate(() => [
    ...(window as unknown as { __rulerSeen: string[] }).__rulerSeen,
  ]);

async function presented(page: Page, frame: number) {
  await expect(page.locator(CANVAS)).toHaveAttribute(
    "data-h3-nle-presented-frame",
    String(frame),
  );
  await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
    "Monitor paused.",
  );
}

test("A64-8: a selection and a trim keep the playhead and the ruler, and a press right after a receipt lands", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
    "Monitor paused.",
    { timeout: 60_000 },
  );
  const ruler = playheadSlider(page);
  await seekPlayhead(page, ruler, 30);
  await presented(page, 30);
  await watchRuler(ruler);

  // A selection: a new revision, the same composition.
  await page
    .locator(
      '[data-h3-nle-clip="clip-0"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await presented(page, 30);
  expect(await playheadFrame(ruler)).toBe(30);

  // A trim: the composition changes; the playhead stays where it was.
  await page.locator('[data-h3-nle-control="clip.trim_end_playhead"]').click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  // No settling wait: the press goes in straight after the receipt.
  const pressed = await pressRuler(page, ruler, 12);
  await expect.poll(() => playheadFrame(ruler)).toBe(pressed);
  await presented(page, pressed);

  const seen = await rulerSeen(page);
  expect(
    seen.filter((entry) => entry.startsWith("true")),
    seen.join(" "),
  ).toEqual([]);
  // The playhead never passed through frame 0 on the way.
  expect(
    seen.filter((entry) => entry.endsWith(":0")),
    seen.join(" "),
  ).toEqual([]);
});

// B-M2564-04: a selection accepted while the first owner is still opening starts a rebind with no
// kept position. A ruler press then is held for the new owner and settled at once, so the frame it
// asked for must be the transport's frame straight away. It was not: the timeline cleared its
// requested frame, rendered its tools from no playhead, and Split stayed unavailable after the
// frame arrived, because transport frames reach the timeline without a render. Videos are delayed
// so the press always lands inside that rebind.
test("B-M2564-04: a press held by a rebind begun during the first opening is the edit position", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page, { delayMs: { video: 1500 } });
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  const monitor = page.locator('[data-h3-nle-status="monitor"]');
  await expect(monitor).toHaveText("Opening monitor.");
  await page
    .locator(
      '[data-h3-nle-clip="clip-2"] [data-h3-nle-control="selection.set"]',
    )
    .focus();
  await page.keyboard.press("Space");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  const clip = (await snapshot(page)).timelineSnapshot!.clips.find(
    (member) => member.clipId === "clip-2",
  )!;
  const ruler = playheadSlider(page);
  await expect(ruler).toHaveAttribute("aria-disabled", "false");
  await expect(monitor).toHaveText("Opening monitor.");
  const pressed = await pressRuler(page, ruler, clip.startFrame + 12);
  const split = page.locator('[data-h3-nle-control="clip.split"]');
  await expect(split).not.toHaveAttribute("aria-disabled", "true");
  await presented(page, pressed);
  await expect(split).not.toHaveAttribute("aria-disabled", "true");
  await split.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  const clips = (await snapshot(page)).timelineSnapshot!.clips;
  expect(clips.find((member) => member.clipId === "clip-2")).toMatchObject({
    startFrame: clip.startFrame,
    durationFrames: 12,
  });
});

// B-M2564-09: the rebind held seeks and Play for the new owner but passed a frame step straight
// to the session, whose step pauses first. Pausing an owner that is still opening is refused and
// the session latched source_unavailable, so a `.` inside the rebind window after a selection left
// the monitor "Preview unavailable" with the transport disabled. Delay authority rebind itself:
// a retained decoder needs no video fetch, and keeps the monitor paused during that window.
test("B-M2564-09: a frame step inside the rebind window lands and the monitor stays available", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page, { delayMs: { video: 1500 } });
  await canonicalWorkspace(
    page,
    undefined,
    undefined,
    "&media=1&rebindDelayMs=1500",
  );
  const monitor = page.locator('[data-h3-nle-status="monitor"]');
  await expect(monitor).toHaveText("Monitor paused.", { timeout: 30_000 });
  await page
    .locator(
      '[data-h3-nle-clip="clip-2"] [data-h3-nle-control="selection.set"]',
    )
    .focus();
  await page.keyboard.press("Space");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.rebindsInFlight)
    .toBe(1);
  await expect(monitor).toHaveText("Monitor paused.");
  const ruler = playheadSlider(page);
  expect(await playheadFrame(ruler)).toBe(0);
  await page.locator("[data-h3-nle-root] .h3-nle-monitor").focus();
  await page.keyboard.press(".");
  // The step is held like a ruler press: the transport reports it at once.
  await expect.poll(() => playheadFrame(ruler)).toBe(1);
  // The retained owner is sought after authority settles; the monitor never becomes unavailable.
  await expect
    .poll(async () => (await snapshot(page)).mediaOwnership.rebindsInFlight)
    .toBe(0);
  await expect(monitor).toHaveText("Monitor paused.", { timeout: 30_000 });
  expect(await playheadFrame(ruler)).toBe(1);
  await expect(
    page.getByRole("button", { name: "Retry", exact: true }),
  ).toHaveCount(0);
  const split = page.locator('[data-h3-nle-control="clip.split"]');
  await expect(split).not.toHaveAttribute("aria-disabled", "true");
});

// B-M2564-05: the timeline's tools follow the playhead wherever it moves. Transport frames reach
// the timeline through its channel without a render (so playback does not re-render the editor),
// and the toolbar's states were computed only at render: after a frame step or playback from the
// monitor's own transport, Split kept the state of the frame the timeline last rendered.
test("B-M2564-05: Split follows frame steps and playback from the monitor's transport", async ({
  page,
}) => {
  await routeGenericFixtureMedia(page);
  await canonicalWorkspace(page, undefined, undefined, "&media=1");
  const monitor = page.locator('[data-h3-nle-status="monitor"]');
  await expect(monitor).toHaveText("Monitor paused.", { timeout: 30_000 });
  await page
    .locator(
      '[data-h3-nle-clip="clip-2"] [data-h3-nle-control="selection.set"]',
    )
    .focus();
  await page.keyboard.press("Space");
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(1);
  await expect(monitor).toHaveText("Monitor paused.");
  const ruler = playheadSlider(page);
  const split = page.locator('[data-h3-nle-control="clip.split"]');
  // Frame 0 is clip-2's first frame: nothing to split there.
  expect(await playheadFrame(ruler)).toBe(0);
  await expect(split).toHaveAttribute("aria-disabled", "true");
  // One frame on, by the monitor's own key.
  await page.locator("[data-h3-nle-root] .h3-nle-monitor").focus();
  await page.keyboard.press(".");
  await expect.poll(() => playheadFrame(ruler)).toBe(1);
  await expect(split).not.toHaveAttribute("aria-disabled", "true");
  // One frame back, to the clip's start, then play into it and pause.
  await page.keyboard.press(",");
  await expect.poll(() => playheadFrame(ruler)).toBe(0);
  await expect(split).toHaveAttribute("aria-disabled", "true");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect.poll(() => playheadFrame(ruler)).toBeGreaterThan(2);
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(monitor).toHaveText("Monitor paused.");
  const frame = await playheadFrame(ruler);
  expect(frame).toBeGreaterThan(0);
  expect(frame).toBeLessThan(48);
  await expect(split).not.toHaveAttribute("aria-disabled", "true");
  await split.click();
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(2);
  expect(
    (await snapshot(page)).timelineSnapshot!.clips.find(
      (member) => member.clipId === "clip-2",
    ),
  ).toMatchObject({ startFrame: 0, durationFrames: frame });
});

// B-M2564-12: an accepted edit rebinds the monitor with the playhead kept (B-M2561-04), and the new
// owner presents frame 0 before it is sought back. When the kept frame is inside a selected clip
// that frame 0 does not show, the transform overlay needs the kept frame's presentation to render;
// the monitor published nothing then, because the transport's frame had not changed, and the
// overlay stayed absent until the playhead moved. Found by the supplied-host reference-fidelity row
// (`6d665dc7-h53-01-02`, `a8edae12-h53-01-03`).
test("B-M2564-12: the transform overlay returns after a rebind that keeps the playhead inside the selected clip", async ({
  page,
}) => {
  await openIntegratedShell(page, "smoke");
  await expect(page.locator('[data-h3-nle-status="monitor"]')).toHaveText(
    "Monitor paused.",
    { timeout: 60_000 },
  );
  const clip = (await shellSnapshot(page)).timelineSnapshot!.clips.find(
    (member) => member.clipId === "clip-6",
  )!;
  // clip-6 is an image overlay that frame 0 does not show.
  expect(clip.startFrame).toBeGreaterThan(0);
  const ruler = playheadSlider(page);
  const target = clip.startFrame + 20;
  await seekPlayhead(page, ruler, target);
  await presented(page, target);
  await watchRuler(ruler);
  // A selection is an accepted edit: the monitor rebinds and keeps the playhead at `target`.
  await page
    .locator(
      '[data-h3-nle-clip="clip-6"] [data-h3-nle-control="selection.set"]',
    )
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await expect(
    page.locator('[data-h3-nle-transform-overlay="clip-6"]'),
  ).toBeVisible({ timeout: 15_000 });
  await presented(page, target);
  // Nothing moved the playhead: the overlay came back with the rebind itself.
  const seen = await rulerSeen(page);
  expect(
    seen.filter((entry) => !entry.endsWith(`:${target}`)),
    seen.join(" "),
  ).toEqual([]);
});
