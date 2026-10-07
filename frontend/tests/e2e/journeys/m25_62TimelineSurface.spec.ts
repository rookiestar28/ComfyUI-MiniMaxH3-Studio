/**
 * M25-62 redesigned timeline surface, measured in the rendered editor:
 *
 * - A62-1: the toolbar keeps its 12 controls in order, in an edit and a view group on one row at
 *   720 px, borderless at the icon target, with the shortcut in each description.
 * - A62-2: track headers show display names and working toggles in 112 px under a fine pointer;
 *   a coarse pointer keeps the 160 px header with three disjoint 44 px targets (DD-8).
 * - A62-3: the playhead layer spans the ruler top to the lane bottom (one row, and many rows
 *   scrolled), paints at frame 0 in both bands, lets a click reach the clip beneath, and its head
 *   seeks through the ruler path and restores on cancel.
 * - A62-4: a 24 px clip keeps both grips inside itself while its neighbours stay theirs; at
 *   16 px/f an Arrow moves an edge one frame; below 24 px one grip and the rail remain; the roll
 *   target is centred on its cut at the mode's size (B-M2562-07).
 * - A62-5: the label strip and the selection ring render.
 * - A62-6: the scroll bar appears only when the extent exceeds the view and moves `viewStart`
 *   within its clamp, while `transport.scroll` still works.
 * - A62-7: the empty Main lane shows the drop zone (the drop itself is the bin journey).
 */

import { expect, test, type Page } from "@playwright/test";

import { routeGenericFixtureMedia } from "../helpers/genericFixtureMedia";
import { canonicalWorkspace, snapshot, surface } from "../helpers/nleCanonical";
import {
  playheadFrame,
  playheadSlider,
  seekPlayhead,
} from "../helpers/nleTimeline";
import {
  expectGripClearOfLanes,
  nleLaneOrigin,
  nleTargetFloor,
} from "../helpers/nleTargets";

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

type Box = { x: number; y: number; width: number; height: number };
type Wire = Record<string, unknown>;

const timeline = (page: Page) =>
  page.locator(surface).locator('[data-h3-nle-region="timeline"]');

async function box(page: Page, selector: string): Promise<Box> {
  const found = await page
    .locator(surface)
    .locator(selector)
    .first()
    .boundingBox();
  if (found === null) throw new Error(`no box for ${selector}`);
  return found;
}

async function scale(page: Page): Promise<number> {
  return Number(
    await timeline(page).getAttribute("data-h3-nle-pixels-per-frame"),
  );
}

async function viewStart(page: Page): Promise<number> {
  return Number(await timeline(page).getAttribute("data-h3-nle-view-start"));
}

async function receipts(page: Page, expected: number) {
  await expect.poll(async () => (await snapshot(page)).receipts).toBe(expected);
}

/** The screenshot colour of one CSS pixel, decoded in the page (no image library needed). */
async function pixel(page: Page, x: number, y: number) {
  const png = await page.screenshot({
    clip: { x: Math.floor(x), y: Math.floor(y), width: 1, height: 1 },
  });
  return page.evaluate(async (data) => {
    const image = new Image();
    image.src = `data:image/png;base64,${data}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const context = canvas.getContext("2d")!;
    context.drawImage(image, 0, 0);
    return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
  }, png.toString("base64"));
}

/** A role token as the page resolves it, e.g. `--h3-nle-playhead` -> [232, 177, 58]. */
async function roleColour(page: Page, token: string) {
  return page.locator(surface).evaluate((element, name) => {
    const probe = document.createElement("span");
    probe.style.color = `var(${name})`;
    element.append(probe);
    const [r, g, b] = getComputedStyle(probe).color.match(/\d+/gu)!.map(Number);
    probe.remove();
    return [r!, g!, b!];
  }, token);
}

function near(actual: number[], expected: number[], tolerance = 24) {
  return actual.every(
    (channel, index) => Math.abs(channel - expected[index]!) <= tolerance,
  );
}

/**
 * Exact scales: the slider reaches 16 only to within a float step, the zoom-in button clamps at
 * exactly 16, and each zoom-out halves it. At 16 already, the slider sits at 1000 and a fill is a
 * no-op, so it is skipped.
 */
async function zoomExactly(page: Page, pixelsPerFrame: 16 | 8 | 4 | 2 | 1) {
  if ((await scale(page)) !== 16) {
    await page
      .locator('[data-h3-nle-control="transport.zoom_continuous"]')
      .fill("1000");
    await expect.poll(() => scale(page)).toBeGreaterThan(15.9);
    if ((await scale(page)) < 16)
      await page.locator('[data-h3-nle-control="transport.zoom_in"]').click();
  }
  await expect.poll(() => scale(page)).toBe(16);
  for (let current = 16; current > pixelsPerFrame; current /= 2)
    await page.locator('[data-h3-nle-control="transport.zoom_out"]').click();
  await expect.poll(() => scale(page)).toBe(pixelsPerFrame);
}

/** Scroll the view back to frame 0 through the accessible `transport.scroll` control. */
async function scrollToStart(page: Page) {
  const more = page.locator('[data-h3-nle-control="toolbar.more"]');
  await more.click();
  await page.locator('[data-h3-nle-control="transport.scroll"]').fill("0");
  await page.keyboard.press("Escape");
  await expect.poll(() => viewStart(page)).toBe(0);
}

/** One primary track holding clip-a [0,24), clip-b [24,27), clip-c [27,51); others emptied. */
function threeAdjacentClips(wire: Wire) {
  const clips = wire.clips as Wire[];
  const template = clips.find((clip) => clip.track_id === "track-0")!;
  wire.clips = [
    ["clip-a", 0, 24],
    ["clip-b", 24, 3],
    ["clip-c", 27, 24],
  ].map(([id, start, duration]) => ({
    ...structuredClone(template),
    clip_id: id,
    start_frame: start,
    duration_frames: duration,
  }));
}

/** One primary track holding clip-a [0,24) and clip-c [24,48); others emptied. */
function twoAdjacentClips(wire: Wire) {
  threeAdjacentClips(wire);
  wire.clips = (wire.clips as Wire[])
    .filter((clip) => clip.clip_id !== "clip-b")
    .map((clip) =>
      clip.clip_id === "clip-c" ? { ...clip, start_frame: 24 } : clip,
    );
}

/**
 * B-M2562-07: the roll target of a selected clip's cut is centred on that cut and is the element
 * a press there reaches. The roll tests that drive the control activate it wherever it sits, so
 * without this a target drawn one header width left of its cut still passed them.
 */
async function expectRollTargetOnCut(page: Page) {
  await canonicalWorkspace(page, twoAdjacentClips);
  await zoomExactly(page, 8);
  await scrollToStart(page);
  const a = await box(page, '[data-h3-nle-clip="clip-a"]');
  const c = await box(page, '[data-h3-nle-clip="clip-c"]');
  expect(c.x).toBeCloseTo(a.x + a.width, 0);
  await page.mouse.click(a.x + a.width / 2, a.y + a.height / 2);
  await receipts(page, 1);
  const roll = page
    .locator(surface)
    .locator(
      '[data-h3-nle-control="boundary.roll"][data-h3-nle-roll-left="clip-a"][data-h3-nle-roll-right="clip-c"]',
    );
  await expect(roll).toBeVisible();
  const target = (await roll.boundingBox())!;
  // Fine 24 px (the minimum), coarse 44 px: the dialog-wide 30 px button floor must not win.
  const size = await nleTargetFloor(page);
  expect(target.width).toBeCloseTo(size, 0);
  expect(target.height).toBeCloseTo(size, 0);
  expect(target.x + target.width / 2).toBeCloseTo(c.x, 0);
  const row = await box(page, '[data-h3-nle-track="track-0"]');
  expect(target.y).toBeGreaterThanOrEqual(row.y - 0.5);
  expect(target.y + target.height).toBeLessThanOrEqual(
    row.y + row.height + 0.5,
  );
  expect(
    await roll.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2,
      );
      return hit !== null && element.contains(hit);
    }),
  ).toBe(true);
}

function oneTrack(wire: Wire) {
  wire.tracks = (wire.tracks as Wire[]).filter(
    (track) => track.kind === "primary_video",
  );
  wire.clips = (wire.clips as Wire[]).filter(
    (clip) => clip.track_id === "track-0",
  );
}

test("A62-1: the toolbar keeps 12 ordered controls in two borderless groups on one row at 720 px", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await canonicalWorkspace(page);
  const toolbar = page
    .locator(surface)
    .getByRole("toolbar", { name: "Timeline tools" });
  const items = await toolbar
    .locator("[data-h3-nle-toolbar-item]")
    .evaluateAll((elements) =>
      elements.map((element) => {
        const bounds = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          control: (element as HTMLElement).dataset.h3NleControl,
          top: Math.round(bounds.top),
          left: bounds.left,
          right: bounds.right,
          width: bounds.width,
          height: bounds.height,
          border: style.borderTopWidth,
          background: style.backgroundColor,
        };
      }),
    );
  expect(items.map((item) => item.control)).toEqual([
    "history.undo",
    "history.redo",
    "clip.split",
    "clip.trim_start_playhead",
    "clip.trim_end_playhead",
    "clip.remove",
    "transport.snap",
    "transport.ripple",
    "transport.zoom_out",
    "transport.zoom_in",
    "transport.zoom_fit",
    "toolbar.more",
  ]);
  expect(new Set(items.map((item) => item.top)).size).toBe(1);
  const icon = await nleTargetFloor(page, "icon");
  for (const item of items) {
    expect(item.width, item.control).toBeCloseTo(icon, 0);
    expect(item.height, item.control).toBeCloseTo(icon, 0);
    expect(item.border, item.control).toBe("0px");
  }
  // The view group is pushed right: a clear gap after Delete, and More ends at the toolbar edge.
  const del = items.find((item) => item.control === "clip.remove")!;
  const snap = items.find((item) => item.control === "transport.snap")!;
  expect(snap.left - del.right).toBeGreaterThan(16);
  const toolbarBox = (await toolbar.boundingBox())!;
  expect(
    toolbarBox.x + toolbarBox.width - items.at(-1)!.right,
  ).toBeLessThanOrEqual(9);
  const separators = await toolbar
    .locator('[data-h3-nle-toolbar-break="separator"]')
    .evaluateAll((slots) =>
      slots.map((slot) => getComputedStyle(slot, "::before").width),
    );
  expect(separators).toEqual(["1px", "1px", "1px"]);
  await toolbar.locator('[data-h3-nle-control="clip.split"]').focus();
  await expect(
    page.locator(surface).getByRole("tooltip").filter({ hasText: "Ctrl+B" }),
  ).toBeVisible();
});

test("A62-2: track headers show display names and toggle lock and visibility by pointer and keyboard", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const headers = page.locator(surface).locator(".h3-nle-track-header");
  await expect(headers).toHaveCount(4);
  expect(await headers.locator(".h3-nle-track-name").allTextContents()).toEqual(
    ["Main", "Overlay 1", "Overlay 2", "Text 1"],
  );
  const origin = await nleLaneOrigin(page);
  expect(origin).toBe(112 + 16);
  for (const header of await headers.all()) {
    const outer = (await header.boundingBox())!;
    expect(outer.width).toBeCloseTo(112, 0);
    for (const child of await header.locator(":scope > button").all()) {
      const inner = (await child.boundingBox())!;
      expect(inner.x).toBeGreaterThanOrEqual(outer.x - 0.5);
      expect(inner.x + inner.width).toBeLessThanOrEqual(
        outer.x + outer.width + 0.5,
      );
      expect(inner.y).toBeGreaterThanOrEqual(outer.y - 0.5);
      expect(inner.y + inner.height).toBeLessThanOrEqual(
        outer.y + outer.height + 0.5,
      );
    }
    for (const toggle of await header.locator(".h3-nle-track-toggle").all()) {
      const size = (await toggle.boundingBox())!;
      expect(size.width).toBeCloseTo(24, 0);
      expect(size.height).toBeCloseTo(24, 0);
    }
  }
  const main = page.locator(surface).locator('[data-h3-nle-track="track-0"]');
  const lock = main.locator('[data-h3-nle-control="track.locked"]');
  const eye = main.locator('[data-h3-nle-control="track.enabled"]');
  await expect(lock).toHaveAccessibleName("Lock Main");
  await expect(eye).toHaveAccessibleName("Show Main");
  // Visibility first: the core refuses `set_track_enabled` on a locked track by design.
  await eye.focus();
  await page.keyboard.press("Space");
  await receipts(page, 1);
  await expect(eye).toHaveAttribute("aria-pressed", "false");
  await lock.click();
  await receipts(page, 2);
  await expect(lock).toHaveAttribute("aria-pressed", "true");
  const tracks = (await snapshot(page)).timelineSnapshot!.tracks;
  expect(tracks.find((track) => track.trackId === "track-0")).toMatchObject({
    locked: true,
    enabled: false,
  });
});

test.describe("coarse pointer", () => {
  test.use({ hasTouch: true });

  // B-M2562-01: a bounding box alone is not a target. The first version of this case measured
  // 44 px boxes while the toggles covered the trigger's centre, so a tap on the name hit Lock.
  // Each target is now hit-tested at its own centre and the three are pairwise disjoint.
  test("A62-2: a coarse header keeps today's 160 px so the menu target and both toggles are disjoint 44 px targets", async ({
    page,
  }) => {
    await canonicalWorkspace(page);
    expect(
      await page.evaluate(() => matchMedia("(pointer: coarse)").matches),
    ).toBe(true);
    expect(await nleLaneOrigin(page)).toBe(160 + 16);
    for (const header of await page
      .locator(surface)
      .locator(".h3-nle-track-header")
      .all()) {
      // Lower rows sit below the tracks scroller's fold; hit-test each where a user can reach it.
      await header.scrollIntoViewIfNeeded();
      const outer = (await header.boundingBox())!;
      expect(outer.width).toBeCloseTo(160, 0);
      const targets = [
        header.locator('[data-h3-nle-menu-trigger="track"]'),
        header.locator('[data-h3-nle-control="track.locked"]'),
        header.locator('[data-h3-nle-control="track.enabled"]'),
      ];
      const boxes = [];
      for (const target of targets) {
        const box = (await target.boundingBox())!;
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
        expect(box.x).toBeGreaterThanOrEqual(outer.x - 0.5);
        expect(box.x + box.width).toBeLessThanOrEqual(
          outer.x + outer.width + 0.5,
        );
        // Names what the centre hits, so a failure says which element covers the target.
        expect(
          await target.evaluate((element) => {
            const rect = element.getBoundingClientRect();
            const hit = document.elementFromPoint(
              rect.left + rect.width / 2,
              rect.top + rect.height / 2,
            );
            if (hit !== null && element.contains(hit)) return "self";
            return hit === null
              ? "nothing"
              : `${hit.tagName.toLowerCase()}.${hit.className} ${hit.getAttribute("data-h3-nle-control") ?? ""}`;
          }),
        ).toBe("self");
        boxes.push(box);
      }
      for (let a = 0; a < boxes.length; a += 1)
        for (let b = a + 1; b < boxes.length; b += 1)
          expect(
            boxes[a].x + boxes[a].width <= boxes[b].x + 0.5 ||
              boxes[b].x + boxes[b].width <= boxes[a].x + 0.5,
          ).toBe(true);
      await expect(targets[0]).toHaveAccessibleName(
        /^Open track menu: (Main|Overlay \d|Text \d)$/u,
      );
    }
    // The wider header moves frame 0 with it: the first clip starts at the lead-in.
    const main = page.locator(surface).locator('[data-h3-nle-track="track-0"]');
    const header = (await main.locator(".h3-nle-track-header").boundingBox())!;
    const first = (await main
      .locator("[data-h3-nle-clip]")
      .first()
      .boundingBox())!;
    expect(first.x - (header.x + header.width)).toBeCloseTo(16, 0);
    await main.locator('[data-h3-nle-menu-trigger="track"]').click();
    await expect(page.getByRole("menu", { name: "Track menu" })).toBeVisible();
  });

  test("A62-4: the 44 px roll target is centred on the selected clip's cut", async ({
    page,
  }) => {
    await expectRollTargetOnCut(page);
  });
});

test.describe("A62-3 playhead layer", () => {
  async function layerMatchesSurface(page: Page) {
    const layer = await box(page, ".h3-nle-playhead-layer");
    const ruler = await box(page, ".h3-nle-ruler");
    const tracks = await box(page, ".h3-nle-tracks");
    const line = await box(page, ".h3-nle-playhead");
    expect(layer.y).toBeCloseTo(ruler.y, 0);
    expect(layer.y + layer.height).toBeCloseTo(tracks.y + tracks.height, 0);
    expect(layer.x).toBeCloseTo(tracks.x + 112, 0);
    expect(line.y).toBeCloseTo(layer.y, 0);
    expect(line.height).toBeCloseTo(layer.height, 0);
    return { layer, ruler, tracks, line };
  }

  test("spans the ruler top to the lane bottom with one track", async ({
    page,
  }) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(page, oneTrack, undefined, "&media=1");
    const slider = playheadSlider(page);
    await seekPlayhead(page, slider, 0);
    await layerMatchesSurface(page);
  });

  test("spans it with many tracks scrolled, where the scroller cannot shorten it", async ({
    page,
  }) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(
      page,
      undefined,
      undefined,
      "&media=1&shape=virtualized",
    );
    const slider = playheadSlider(page);
    await seekPlayhead(page, slider, 0);
    const grid = page.locator(surface).locator(".h3-nle-tracks");
    await grid.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    await expect
      .poll(() => grid.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(0);
    await layerMatchesSurface(page);
  });

  test("paints at frame 0 in the ruler band and the lane band, and lets a click reach the clip beneath", async ({
    page,
  }) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(page, undefined, undefined, "&media=1");
    const slider = playheadSlider(page);
    await seekPlayhead(page, slider, 0);
    const { ruler, tracks, line } = await layerMatchesSurface(page);
    const origin = await nleLaneOrigin(page);
    // The line's left edge is the frame-0 boundary, one lead-in right of the header.
    expect(line.x - tracks.x).toBeCloseTo(origin, 0);
    const colour = await roleColour(page, "--h3-nle-playhead");
    const inRuler = await pixel(page, line.x + 1, ruler.y + ruler.height - 4);
    const inLane = await pixel(page, line.x + 1, tracks.y + 28);
    expect(near(inRuler, colour), `ruler ${inRuler} vs ${colour}`).toBe(true);
    expect(near(inLane, colour), `lane ${inLane} vs ${colour}`).toBe(true);

    // Put the playhead inside clip-4 (track 0, frames 180..228) and click on the line there.
    await seekPlayhead(page, slider, 204);
    await expect
      .poll(async () => (await box(page, ".h3-nle-playhead")).x)
      .toBeGreaterThan(line.x + 20);
    const moved = await box(page, ".h3-nle-playhead");
    const clip = await box(page, '[data-h3-nle-clip="clip-4"]');
    expect(moved.x).toBeGreaterThan(clip.x);
    expect(moved.x).toBeLessThan(clip.x + clip.width);
    const before = (await snapshot(page)).receipts;
    await page.mouse.click(moved.x + 1, clip.y + clip.height / 2);
    await receipts(page, before + 1);
    expect((await snapshot(page)).intents.at(-1)!.commands[0]).toMatchObject({
      kind: "select_clips",
      payload: { clip_ids: ["clip-4"] },
    });
  });

  test("drags through the ruler seek path from the head, and restores the frame on cancel", async ({
    page,
  }) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(page, undefined, undefined, "&media=1");
    await zoomExactly(page, 16);
    await scrollToStart(page);
    const slider = playheadSlider(page);
    await seekPlayhead(page, slider, 0);
    const head = page.locator(surface).locator("[data-h3-nle-playhead-head]");
    await expect(head).toHaveAttribute("aria-hidden", "true");
    const grab = (await head.boundingBox())!;
    const x = grab.x + grab.width / 2;
    const y = grab.y + grab.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 96, y, { steps: 4 });
    await expect.poll(() => playheadFrame(slider)).toBe(6);
    await page.mouse.up();
    await page.waitForTimeout(300);
    expect(await playheadFrame(slider)).toBe(6);

    // Cancel: the drag returns to where it started, and later moves do nothing.
    const again = (await head.boundingBox())!;
    const x2 = again.x + again.width / 2;
    await page.mouse.move(x2, y);
    await page.mouse.down();
    await page.mouse.move(x2 + 64, y, { steps: 4 });
    await expect.poll(() => playheadFrame(slider)).toBe(10);
    await head.dispatchEvent("pointercancel", {
      pointerId: 1,
      isPrimary: true,
    });
    await expect.poll(() => playheadFrame(slider)).toBe(6);
    await page.mouse.move(x2 + 160, y, { steps: 4 });
    await page.mouse.up();
    await page.waitForTimeout(300);
    expect(await playheadFrame(slider)).toBe(6);
  });

  // B-M2562-02: the head covers +-5.5 px around the playhead, so at 1 px/f a ruler press there
  // lands on the head. It must behave as a ruler press: seek to the pointer, then follow it.
  // Kept a grab offset, the first version seeked nothing on the press and lagged by 4 frames.
  test("a press beside the head's centre seeks to the pointer, as the ruler does", async ({
    page,
  }) => {
    await routeGenericFixtureMedia(page);
    await canonicalWorkspace(page, undefined, undefined, "&media=1");
    await zoomExactly(page, 1);
    await scrollToStart(page);
    const slider = playheadSlider(page);
    await seekPlayhead(page, slider, 20);
    const head = page.locator(surface).locator("[data-h3-nle-playhead-head]");
    const ruler = (await playheadSlider(page).boundingBox())!;
    const origin = await nleLaneOrigin(page);
    // Frame f's pixel starts at ruler.x + origin + f at 1 px/f; the head is centred on the 2 px
    // line, whose centre is that boundary plus 1.
    const rulerX = (frame: number) => ruler.x + origin + frame;
    await expect
      .poll(async () => {
        const box = (await head.boundingBox())!;
        return box.x + box.width / 2;
      })
      .toBeCloseTo(rulerX(20) + 1, 0);
    const y = ruler.y + 6;
    const pressX = rulerX(24) + 0.25;
    // The press must land on the head itself, or the case proves the ruler instead.
    expect(
      await page.evaluate(
        ([x, py]) =>
          document
            .elementFromPoint(x, py)
            ?.closest("[data-h3-nle-playhead-head]") !== null,
        [pressX, y] as const,
      ),
    ).toBe(true);
    await page.mouse.move(pressX, y);
    await page.mouse.down();
    await expect.poll(() => playheadFrame(slider)).toBe(24);
    await page.mouse.move(rulerX(32) + 0.25, y, { steps: 2 });
    await expect.poll(() => playheadFrame(slider)).toBe(32);
    await page.mouse.up();
  });
});

test("A62-4: a 24 px clip keeps both grips inside itself; its neighbours stay theirs; Arrow moves one frame at 16 px/f; below 24 px one grip and the rail remain", async ({
  page,
}) => {
  await canonicalWorkspace(page, threeAdjacentClips);
  await zoomExactly(page, 8);
  await scrollToStart(page);
  const clipBox = (id: string) => box(page, `[data-h3-nle-clip="${id}"]`);
  const b = await clipBox("clip-b");
  expect(b.width).toBeCloseTo(24, 1);
  await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
  await receipts(page, 1);
  const selected = page.locator(surface).locator('[data-h3-nle-clip="clip-b"]');
  await expect(selected).toHaveAttribute("data-h3-nle-inline-grips", "true");
  const grips = selected.locator(".h3-nle-grip:not([hidden])");
  await expect(grips).toHaveCount(2);
  for (const grip of await grips.all()) {
    const g = (await grip.boundingBox())!;
    expect(g.width).toBeCloseTo(8, 0);
    expect(g.x).toBeGreaterThanOrEqual(b.x - 0.5);
    expect(g.x + g.width).toBeLessThanOrEqual(b.x + b.width + 0.5);
  }
  const a = await clipBox("clip-a");
  const c = await clipBox("clip-c");
  const hit = (x: number, y: number) =>
    page.evaluate(
      ([px, py]) => {
        const element = document.elementFromPoint(px!, py!);
        return {
          clip: element
            ?.closest("[data-h3-nle-clip]")
            ?.getAttribute("data-h3-nle-clip"),
          role: element?.classList.contains("h3-nle-grip")
            ? `grip:${element.getAttribute("data-h3-nle-trim-edge")}`
            : element?.classList.contains("h3-nle-clip-body")
              ? "body"
              : (element?.className ?? ""),
        };
      },
      [x, y],
    );
  const middle = b.y + b.height / 2;
  expect(await hit(a.x + a.width - 1, middle)).toEqual({
    clip: "clip-a",
    role: "body",
  });
  expect(await hit(c.x + 1, middle)).toEqual({ clip: "clip-c", role: "body" });
  expect(await hit(b.x + 4, middle)).toEqual({
    clip: "clip-b",
    role: "grip:start",
  });
  expect(await hit(b.x + b.width - 4, middle)).toEqual({
    clip: "clip-b",
    role: "grip:end",
  });

  // 16 px/f: each Arrow on a grip moves that edge by exactly one frame.
  await zoomExactly(page, 16);
  await scrollToStart(page);
  const wide = await clipBox("clip-b");
  const end = selected.locator('[data-h3-nle-trim-edge="end"]');
  await end.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(async () => (await clipBox("clip-b")).width)
    .toBeCloseTo(wide.width + 16, 0);
  await page.keyboard.press("Escape");
  await expect
    .poll(async () => (await clipBox("clip-b")).width)
    .toBeCloseTo(wide.width, 0);
  const start = selected.locator('[data-h3-nle-trim-edge="start"]');
  await start.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(async () => (await clipBox("clip-b")).x)
    .toBeCloseTo(wide.x + 16, 0);
  await page.keyboard.press("Escape");

  // Below 24 px: the end grip stays, and the rail owns both edges and the menu.
  await zoomExactly(page, 4);
  await scrollToStart(page);
  expect((await clipBox("clip-b")).width).toBeCloseTo(20, 0);
  await expect(selected).toHaveAttribute("data-h3-nle-inline-grips", "single");
  await expect(selected.locator(".h3-nle-grip:not([hidden])")).toHaveCount(1);
  const rail = page.locator(surface).locator(".h3-nle-trim-rail");
  await expect(rail).toHaveAttribute("data-h3-nle-trim-clip", "clip-b");
  await expect(rail.locator(".h3-nle-rail-label")).toHaveText(
    "Clip 01 · 00:00:01:00–00:00:01:03",
  );
  const railText = `${await rail.textContent()} ${await rail.getAttribute("aria-label")}`;
  expect(railText).not.toContain("clip-b");
  expect(railText).not.toMatch(/\[\s*\d+\s*,/u);
  // The fine rail's edges and menu are full icon targets (30 px); the 44 px rail is the coarse
  // rule, proven by nleWorkspace's coarse-pointer rail cases.
  const icon = await nleTargetFloor(page, "icon");
  for (const target of await rail
    .locator(".h3-nle-rail-grip, .h3-nle-rail-menu")
    .all()) {
    const box = (await target.boundingBox())!;
    expect(box.width).toBeGreaterThanOrEqual(icon - 0.5);
    expect(box.height).toBeGreaterThanOrEqual(icon - 0.5);
  }
  expect(
    await rail.locator(".h3-nle-rail-grip, .h3-nle-rail-menu").count(),
  ).toBe(3);
  // B-M2563-09: with the rail under the surface, the lanes stay clear of the corner grip.
  await expectGripClearOfLanes(page);
});

test("A62-4: the roll target is centred on the selected clip's cut", async ({
  page,
}) => {
  await expectRollTargetOnCut(page);
});

// B-M2562-08 (A62-8, the canvas's "Ruler scales"): every whole second is a labelled mark and
// the sub-second labels restart at each second. The A61-2 case checks the label format and spacing
// only, which a ruler reading "00:00 10f 20f 06f 16f" also satisfies.
test("A62-8: the ruler labels every whole second at 16 and 8 px per frame", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const labels = () =>
    page
      .locator(surface)
      .locator(".h3-nle-ruler .h3-nle-ruler-mark")
      .allTextContents();
  await zoomExactly(page, 16);
  await scrollToStart(page);
  expect((await labels()).slice(0, 6)).toEqual([
    "00:00",
    "06f",
    "12f",
    "18f",
    "00:01",
    "06f",
  ]);
  await zoomExactly(page, 8);
  await scrollToStart(page);
  expect((await labels()).slice(0, 5)).toEqual([
    "00:00",
    "12f",
    "00:01",
    "12f",
    "00:02",
  ]);
});

test("A62-5: the label strip, the duration and the selection ring render above the raster", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  await zoomExactly(page, 4);
  await scrollToStart(page);
  const clip = page.locator(surface).locator('[data-h3-nle-clip="clip-0"]');
  await clip.locator(".h3-nle-clip-body").click();
  await receipts(page, 1);
  await expect(clip.locator(".h3-nle-clip-label")).toHaveText("Clip 01");
  await expect(clip.locator(".h3-nle-clip-duration")).toHaveText("00:00:02:00");
  const body = clip.locator(".h3-nle-clip-body");
  const name = await body.getAttribute("aria-label");
  expect(name).toBe("Clip 01, 00:00:00:00–00:00:02:00, 00:00:02:00");
  expect(await body.getAttribute("title")).toBe(name);
  const ring = (await clip.boundingBox())!;
  const selection = await roleColour(page, "--h3-nle-selection");
  const top = await pixel(page, ring.x + ring.width / 2, ring.y + 1);
  expect(near(top, selection), `ring ${top} vs ${selection}`).toBe(true);
  const label = await roleColour(page, "--h3-nle-label-backing");
  expect(label.length).toBe(3);
});

test("A62-6: the scroll bar shows only beyond the view and moves viewStart within its clamp", async ({
  page,
}) => {
  await canonicalWorkspace(page);
  const bar = page
    .locator(surface)
    .locator('[data-h3-nle-control="transport.scroll_bar"]');
  await expect(bar).toHaveAttribute("aria-hidden", "true");
  await page.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  await expect(bar).toBeHidden();
  await zoomExactly(page, 4);
  await scrollToStart(page);
  await expect(bar).toBeVisible();
  const barBox = (await bar.boundingBox())!;
  const lane = await box(page, ".h3-nle-tracks");
  expect(barBox.x).toBeCloseTo(lane.x + 112, 0);
  expect(barBox.height).toBeCloseTo(8, 0);
  const thumb = (await bar.locator(".h3-nle-scroll-thumb").boundingBox())!;
  const y = thumb.y + thumb.height / 2;
  await page.mouse.move(thumb.x + thumb.width / 2, y);
  await page.mouse.down();
  await page.mouse.move(thumb.x + thumb.width / 2 + 200, y, { steps: 4 });
  await page.mouse.up();
  const middle = await viewStart(page);
  expect(middle).toBeGreaterThan(0);
  // Far past the end, the view stops at the same clamp every navigation uses.
  const now = (await bar.locator(".h3-nle-scroll-thumb").boundingBox())!;
  await page.mouse.move(now.x + now.width / 2, y);
  await page.mouse.down();
  await page.mouse.move(now.x + 5_000, y, { steps: 4 });
  await page.mouse.up();
  const clamped = await viewStart(page);
  const more = page.locator('[data-h3-nle-control="toolbar.more"]');
  await more.click();
  const range = page.locator('[data-h3-nle-control="transport.scroll"]');
  expect(Number(await range.getAttribute("max"))).toBeCloseTo(clamped, 3);
  await range.fill("0");
  await page.keyboard.press("Escape");
  await expect.poll(() => viewStart(page)).toBe(0);
  const back = (await bar.locator(".h3-nle-scroll-thumb").boundingBox())!;
  expect(back.x).toBeCloseTo(barBox.x, 0);
});

test("A62-7: an empty Main lane shows the dashed drop zone instead of a sentence", async ({
  page,
}) => {
  await page.goto("/nleWorkspace.html?zeroClips=1");
  await page.getByRole("button", { name: "Open full editor" }).click();
  await expect(page.locator(surface)).toBeVisible();
  const zone = page.locator(surface).locator("[data-h3-nle-empty-lane]");
  // M25-63 updated the sentence for the bin's "+".
  await expect(zone).toHaveText("Drag media here, or press + on a thumbnail");
  const lane = page
    .locator(surface)
    .locator('[data-h3-nle-track="track-0"] .h3-nle-track-lane');
  const zoneBox = (await zone.boundingBox())!;
  const laneBox = (await lane.boundingBox())!;
  expect(zoneBox.y).toBeGreaterThanOrEqual(laneBox.y);
  expect(zoneBox.y + zoneBox.height).toBeLessThanOrEqual(
    laneBox.y + laneBox.height,
  );
  expect(
    await zone.evaluate((element) => getComputedStyle(element).borderTopStyle),
  ).toBe("dashed");
  expect(
    await zone.evaluate((element) => getComputedStyle(element).pointerEvents),
  ).toBe("none");
  // The monitor keeps its own empty-timeline note; the timeline region no longer has one.
  await expect(
    timeline(page).getByText("The timeline has no clips."),
  ).toHaveCount(0);
});
