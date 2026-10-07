// M25-53: one final-tree, browser-layout observation of the rebuilt editor. This journey owns
// the reference geometry and single-editor facts; detailed gesture/command behavior remains in
// the focused owner journeys and the complete 33-kind / 66-row command matrix.

import { expect, test } from "@playwright/test";
import { chooseMediaOption } from "../helpers/nleBinMenus";
import { nleFineGripException, nleTargetFloor } from "../helpers/nleTargets";

import {
  openIntegratedShell,
  shellLastPresentation,
  shellSnapshot,
  shellSurface,
} from "../helpers/nleShell";
import {
  expectNarrowReferenceShell,
  expectStandardReferenceShell,
  referenceShell,
  summarizeReferenceShell,
} from "../helpers/nleReferenceShellHost";
import { playheadSlider, seekPlayhead } from "../helpers/nleTimeline";

test.use({ viewport: { width: 1402, height: 868 }, deviceScaleFactor: 1 });

test("M25-53 final editor preserves reference geometry, picture and one editing surface", async ({
  page,
}, testInfo) => {
  const mediaEvents: string[] = [];
  const browserErrors: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/nle-media/"))
      mediaEvents.push(`${response.status()} ${response.url()}`);
  });
  page.on("requestfailed", (request) => {
    if (request.url().includes("/nle-media/"))
      mediaEvents.push(
        `FAILED ${request.url()} ${request.failure()?.errorText}`,
      );
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  const oracle = await openIntegratedShell(page, "reference-preload");
  const overlay = page.locator(shellSurface);
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await expect(overlay.locator("[data-h3-nle-asset]")).toHaveCount(4);
  await expect(
    overlay.locator('[data-h3-nle-control="media.search"]'),
  ).toBeVisible();
  // M25-63: sort and filter are one menu; its two groups keep their controls.
  await expect(
    overlay.locator('[data-h3-nle-control="media.sort_filter"]'),
  ).toBeVisible();
  await expect(
    overlay.locator('[data-h3-nle-control="media.view.list"]'),
  ).toBeVisible();
  await expect
    .poll(() =>
      overlay
        .locator("[data-h3-nle-thumbnail]")
        .evaluateAll(
          (canvases) =>
            canvases.filter((canvas) => (canvas as HTMLCanvasElement).width > 0)
              .length,
        ),
    )
    .toBeGreaterThanOrEqual(3);
  const mediaCards = overlay.locator("[data-h3-nle-asset]");
  const search = overlay.locator('[data-h3-nle-control="media.search"]');
  await search.fill("Clip 02");
  await expect(mediaCards).toHaveCount(1);
  await expect(mediaCards.first()).toHaveAttribute(
    "data-h3-nle-card-index",
    "2",
  );
  await search.fill("");
  await chooseMediaOption(overlay, "media.filter", "video");
  await expect(mediaCards).toHaveCount(3);
  await chooseMediaOption(overlay, "media.filter", "image", "keyboard");
  await expect(mediaCards).toHaveCount(1);
  await chooseMediaOption(overlay, "media.filter", "all");
  await chooseMediaOption(overlay, "media.sort", "ordinal_desc", "keyboard");
  await expect(mediaCards.first()).toHaveAttribute(
    "data-h3-nle-card-index",
    "4",
  );
  await chooseMediaOption(overlay, "media.sort", "ordinal_asc");
  await overlay.locator('[data-h3-nle-control="media.view.list"]').click();
  await expect(overlay.locator("[data-h3-nle-media-view]")).toHaveAttribute(
    "data-h3-nle-media-view",
    "list",
  );
  await overlay.locator('[data-h3-nle-control="media.view.grid"]').click();
  await expect(overlay.locator("[data-h3-nle-media-view]")).toHaveAttribute(
    "data-h3-nle-media-view",
    "grid",
  );
  await expect(mediaCards).toHaveCount(4);
  await testInfo.attach("m25-53-hermetic-default-owned-shell-1402x868", {
    contentType: "image/png",
    body: await overlay.screenshot(),
  });
  await page.evaluate(() =>
    window.nleShellHarness.applyTimelineCommands(
      window.nleShellHarness.referenceAssemblyCommands(),
    ),
  );
  await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(3);
  // A real sequence often leaves a trailing gap. Seeking there releases the monitor's playback
  // owner, allowing the accepted serial decoration broker to finish filmstrips before review.
  const referencePlayhead = overlay.getByRole("slider", {
    name: "Playhead",
    exact: true,
  });
  await expect(referencePlayhead).toHaveAttribute("aria-disabled", "false", {
    timeout: 20_000,
  });
  await referencePlayhead.focus();
  await page.keyboard.press("End");
  await expect(referencePlayhead).toHaveAttribute("aria-valuenow", "143");
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            window.nleShellHarness.snapshot().mediaOwnership.filmstripAttempts,
        ),
      { timeout: 15_000 },
    )
    .toBeGreaterThanOrEqual(3);
  await referencePlayhead.focus();
  await page.keyboard.press("Home");
  await overlay.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  const sourceClips = (await shellSnapshot(page)).timelineSnapshot!.clips;
  expect(sourceClips.map((clip) => clip.assetId)).toEqual([
    "vid-primary",
    "vid-overlay",
    "vid-timing",
  ]);
  await expect(
    overlay.locator(
      '[data-h3-nle-clip="reference-primary"] .h3-nle-clip-body small',
    ),
  ).toHaveText("00:00:02:00");

  const { standard, splitterMoves } = await expectStandardReferenceShell(
    page,
    overlay,
  );
  expect(standard.stageWidth).toBeGreaterThan(1_000);
  expect(standard.stageHeight).toBeGreaterThan(600);

  // The accepted picture is the compositor's delivered 16:9 canvas inside the largest top-row
  // region. Its backing store and visible box must agree; CSS stretching cannot satisfy this.
  const picture = await overlay
    .locator('[data-h3-nle-canvas="composition"]')
    .evaluate((element) => {
      const canvas = element as HTMLCanvasElement;
      const bounds = canvas.getBoundingClientRect();
      const region = canvas
        .closest('[data-h3-nle-area="monitor"]')!
        .getBoundingClientRect();
      return {
        backing: [canvas.width, canvas.height],
        visible: [bounds.width, bounds.height],
        region: [region.width, region.height],
        legacyEditors: document.querySelectorAll("section.h3a").length,
        editingSurfaces: document.querySelectorAll(
          '[data-h3-nle-surface="overlay_v1"]',
        ).length,
      };
    });
  expect(picture.backing[0] / picture.backing[1]).toBeCloseTo(16 / 9, 3);
  expect(picture.visible[0] / picture.visible[1]).toBeCloseTo(16 / 9, 2);
  expect(picture.visible[0]).toBeLessThanOrEqual(picture.region[0]);
  expect(picture.visible[1]).toBeLessThanOrEqual(picture.region[1]);
  expect(picture.legacyEditors).toBe(0);
  expect(picture.editingSurfaces).toBe(1);

  const presentation = await shellLastPresentation(page);
  expect(presentation).not.toBeNull();
  expect(presentation?.frame).toBe(0);
  expect(presentation?.publicFingerprint).toMatch(/^sha256:[0-9a-f]{64}$/);
  const coloredPixels = await overlay
    .locator('[data-h3-nle-canvas="composition"]')
    .evaluate((node) => {
      const canvas = node as HTMLCanvasElement;
      const pixels = canvas
        .getContext("2d")!
        .getImageData(0, 0, canvas.width, canvas.height).data;
      let count = 0;
      for (let index = 0; index < pixels.length; index += 4) {
        if (
          Math.max(pixels[index]!, pixels[index + 1]!, pixels[index + 2]!) > 120
        )
          count += 1;
      }
      return count;
    });
  expect(coloredPixels).toBeGreaterThan(10);
  const primaryLandmark = await overlay
    .locator('[data-h3-nle-canvas="composition"]')
    .evaluate((node) => {
      const canvas = node as HTMLCanvasElement;
      return [...canvas.getContext("2d")!.getImageData(116, 66, 1, 1).data];
    });
  expect(primaryLandmark[0]).toBeGreaterThan(120);
  expect(primaryLandmark[1]).toBeLessThan(100);

  // The final integrated fixture exposes the complete media/inspector surface, not a screenshot
  // shell or a compact fallback.
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await expect(overlay.locator("[data-h3-nle-asset]")).toHaveCount(4);
  await overlay
    .locator('[data-h3-nle-clip] [data-h3-nle-control="selection.set"]')
    .first()
    .click();
  await chooseMediaOption(overlay, "media.filter", "added");
  await expect(mediaCards).toHaveCount(3);
  await chooseMediaOption(overlay, "media.filter", "all");
  const artBadgeGeometry = await mediaCards.first().evaluate((card) => {
    const art = card
      .querySelector(".h3-nle-media-art")!
      .getBoundingClientRect();
    const added = card
      .querySelector('[data-h3-nle-media-badge="added"]')!
      .getBoundingClientRect();
    const duration = card
      .querySelector('[data-h3-nle-media-badge="duration"]')!
      .getBoundingClientRect();
    return {
      addedInside:
        added.left >= art.left &&
        added.top >= art.top &&
        added.bottom <= art.bottom,
      durationInside:
        duration.right <= art.right &&
        duration.top >= art.top &&
        duration.bottom <= art.bottom,
      badgeOrder: added.left < duration.left,
    };
  });
  expect(artBadgeGeometry).toEqual({
    addedInside: true,
    durationInside: true,
    badgeOrder: true,
  });
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  // Tabs are contextual: a video selection exposes the four applicable groups and, because the
  // selected clip's source has bound audio, its Audio group; the Text group is owned by a title
  // selection and is covered by its dedicated journey.
  await expect(overlay.locator("[data-h3-nle-property-tab]")).toHaveCount(5);
  await expect(overlay.locator("[data-h3-nle-transform-overlay]")).toHaveCount(
    1,
  );
  // A decoded canvas alone is insufficient: the full-area move hit target must leave those
  // pixels visible to the user rather than inherit the editor's opaque button material.
  const transformMaterial = await overlay
    .locator("[data-h3-nle-transform-handle]")
    .evaluateAll((buttons) =>
      buttons.map((button) => {
        const style = getComputedStyle(button);
        return {
          backgroundColor: style.backgroundColor,
          backgroundImage: style.backgroundImage,
          borderWidth: style.borderTopWidth,
          boxShadow: style.boxShadow,
        };
      }),
    );
  expect(transformMaterial).toHaveLength(10);
  for (const material of transformMaterial)
    expect(material).toEqual({
      backgroundColor: "rgba(0, 0, 0, 0)",
      backgroundImage: "none",
      borderWidth: "0px",
      boxShadow: "none",
    });
  await expect(
    overlay.locator('[data-h3-nle-canvas="timeline_decoration"]'),
  ).toHaveCount(1);
  try {
    await expect
      .poll(
        () =>
          overlay
            .locator("[data-h3-nle-thumbnail]")
            .evaluateAll(
              (canvases) =>
                canvases.filter(
                  (canvas) => (canvas as HTMLCanvasElement).width > 0,
                ).length,
            ),
        { timeout: 10_000 },
      )
      .toBeGreaterThanOrEqual(3);
  } catch (error) {
    const ownership = await page.evaluate(
      () => window.nleShellHarness.snapshot().mediaOwnership,
    );
    const cards = await overlay
      .locator("[data-h3-nle-asset]")
      .evaluateAll((nodes) =>
        nodes.map((node) => ({
          asset: node.getAttribute("data-h3-nle-asset"),
          width: (node.querySelector("canvas") as HTMLCanvasElement | null)
            ?.width,
          text: node.querySelector(".h3-nle-thumbnail-status")?.textContent,
        })),
      );
    throw new Error(
      `reference thumbnails did not settle: ${JSON.stringify({ ownership, cards, mediaEvents, browserErrors })}`,
      { cause: error },
    );
  }
  const thumbnailWidths = await overlay
    .locator("[data-h3-nle-thumbnail]")
    .evaluateAll((canvases) =>
      canvases.map((canvas) => (canvas as HTMLCanvasElement).width),
    );
  const mediaOwnership = await page.evaluate(
    () => window.nleShellHarness.snapshot().mediaOwnership,
  );

  // Retain only the extension-owned shell under the synthetic hermetic fixture. This is the
  // current candidate view for visual review, not the owner's original image or host evidence.
  await testInfo.attach("m25-53-hermetic-populated-owned-shell-1402x868", {
    contentType: "image/png",
    body: await overlay.screenshot(),
  });
  const dragHandle = overlay.locator('[data-h3-nle-transform-handle="move"]');
  const dragBox = await dragHandle.boundingBox();
  if (dragBox === null)
    throw new Error("reference transform handle has no box");
  const beforeCancelledDrag = oracle.transactions.length;
  await page.mouse.move(
    dragBox.x + dragBox.width / 2,
    dragBox.y + dragBox.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(
    dragBox.x + dragBox.width / 2 + 24,
    dragBox.y + dragBox.height / 2 + 12,
    { steps: 4 },
  );
  await testInfo.attach("m25-53-hermetic-active-drag-owned-shell-1402x868", {
    contentType: "image/png",
    body: await overlay.screenshot(),
  });
  await page.keyboard.press("Escape");
  await page.mouse.up();
  expect(oracle.transactions.length).toBe(beforeCancelledDrag);

  await referencePlayhead.focus();
  await page.keyboard.press("End");
  await expect(
    overlay.locator('[data-h3-nle-transform-overlay="clip-0"]'),
  ).toHaveCount(0);
  // The alignment buttons are in Transform's secondary group, closed at rest.
  await overlay
    .locator('[data-h3-nle-disclosure="transform.anchor_align"]')
    .click();
  const offPlayheadAlign = overlay.locator(
    '[data-h3-nle-control="transform.align.left"]',
  );
  await expect(offPlayheadAlign).toBeDisabled();
  await expect(offPlayheadAlign).toHaveAttribute(
    "aria-description",
    /playhead/i,
  );
  expect(oracle.transactions.length).toBe(beforeCancelledDrag);

  const reopenAt = async (width: number, height: number) => {
    await overlay.locator('[data-h3-nle-action="close"]').click();
    await expect(overlay).toHaveCount(0);
    await page.setViewportSize({ width, height });
    await page
      .locator("#root")
      .getByRole("button", { name: "Open full editor" })
      .click();
    await expect(overlay).toHaveCount(1);
  };

  await reopenAt(960, 600);
  const compact = await referenceShell(overlay);
  expect(compact.areas).toBe(4);
  expect(compact.separators).toBe(3);
  expect(compact.overflowX).toBeLessThanOrEqual(0);
  expect(compact.bin.width).toBeGreaterThanOrEqual(159.5);
  expect(compact.monitor.width).toBeGreaterThanOrEqual(279.5);
  expect(compact.inspector.width).toBeGreaterThanOrEqual(239.5);

  await reopenAt(720, 480);
  const narrow = await expectNarrowReferenceShell(overlay);
  const floor = await nleTargetFloor(page);
  const grip = await nleFineGripException(page);
  const smallControls = await overlay.evaluate(
    (dialog, { floor, grip }) =>
      [...dialog.querySelectorAll<HTMLElement>("button,[role=tab],select")]
        .filter(
          (element) =>
            element.getClientRects().length > 0 &&
            !element.classList.contains("h3-nle-clip-body"),
        )
        .map((element) => {
          const box = element.getBoundingClientRect();
          // M25-62 (B-M2562-05): only the pinned 8 px fine grip is excused its width.
          const excused =
            grip !== null &&
            element.matches(grip.selector) &&
            Math.round(box.width) === grip.widthPx;
          return { width: excused ? floor : box.width, height: box.height };
        })
        .filter(
          ({ width, height }) => width < floor - 0.5 || height < floor - 0.5,
        ),
    { floor, grip },
  );
  expect(smallControls).toEqual([]);
  // Clip bodies encode time span, so a 24-frame source can be narrower than a static 44 px
  // control at the minimum viewport. Prove that this specific short clip remains actionable.
  const shortClip = overlay.locator(
    '[data-h3-nle-clip="reference-overlay"] [data-h3-nle-control="selection.set"]',
  );
  await shortClip.click();
  await expect(shortClip).toHaveAttribute("aria-pressed", "true");

  await page.evaluate(() => {
    const command = window.nleShellHarness.referenceAssemblyCommands()[0]!;
    if (command.kind !== "insert_asset_clip")
      throw new Error("reference fixture lost its media insertion command");
    const clip = command.payload.clip;
    if (clip === null || typeof clip !== "object" || Array.isArray(clip))
      throw new Error("reference fixture lost its media clip");
    window.nleShellHarness.applyTimelineCommands([
      {
        kind: "insert_asset_clip",
        payload: {
          clip: {
            ...clip,
            clip_id: "reference-one-frame",
            start_frame: 120,
            duration_frames: 1,
          },
        },
      },
    ]);
  });
  const oneFrame = overlay.locator('[data-h3-nle-clip="reference-one-frame"]');
  await expect(oneFrame).toBeVisible();
  await oneFrame.scrollIntoViewIfNeeded();
  const oneFrameHit = await oneFrame.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(
      box.x + box.width / 2,
      box.y + box.height / 2,
    );
    return {
      width: box.width,
      selectable:
        hit?.closest('[data-h3-nle-control="selection.set"]') !== null,
    };
  });
  expect(oneFrameHit.width).toBeGreaterThanOrEqual(20);
  expect(oneFrameHit.selectable).toBe(true);

  await testInfo.attach("m25-53-hermetic-reference-geometry", {
    contentType: "application/json",
    body: Buffer.from(
      JSON.stringify({
        schema: "h3.context.m25_53.hermetic_reference_geometry.v1",
        viewport: [1402, 868],
        standard: summarizeReferenceShell(standard),
        compact: summarizeReferenceShell(compact),
        narrow: summarizeReferenceShell(narrow),
        splitterMoves,
        picture,
        presentation,
        thumbnailWidths,
        mediaOwnership,
      }),
      "utf8",
    ),
  });
  await oracle.close();
});

// M25-53 side-by-side finding: at the owner's 1402 x 868 reference size the timeline's help
// line and the workspace's timeline status were painted over each other at the bottom of R4.
// The region is measured, not inspected. M25-62 (R6) removed the visible help line: the trim
// instructions are a visually hidden description the grips reference. M25-63 (R9) moved the save
// state to the top bar: the timeline status is a visually hidden live region, so R4 holds only the
// timeline section, without a footer, a visible status line or a scroller, and the top bar shows
// the save indicator inside its own row.
test("M25-53 timeline footer lines stay inside R4 without overlapping", async ({
  page,
}) => {
  await openIntegratedShell(page, "reference-preload");
  const overlay = page.locator(shellSurface);
  await expect(
    overlay.locator('[data-h3-nle-status="timeline"]'),
  ).toHaveAttribute("data-h3-nle-authoring", "ready");
  const region = overlay.locator('[data-h3-nle-area="timeline"]');
  const measure = () =>
    region.evaluate((area) => {
      const box = (element: Element | null) => {
        if (element === null) return null;
        const rect = element.getBoundingClientRect();
        return {
          top: Math.round(rect.top),
          bottom: Math.round(rect.bottom),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
        };
      };
      const scroller = area.querySelector<HTMLElement>(".h3-nle-timeline-area");
      return {
        region: box(area),
        scroller:
          scroller === null
            ? null
            : {
                clientHeight: scroller.clientHeight,
                scrollHeight: scroller.scrollHeight,
                rows: getComputedStyle(scroller).gridTemplateRows,
              },
        rail: box(area.querySelector(".h3-nle-trim-rail")),
        section: box(area.querySelector(".h3-nle-timeline")),
        toolbar: box(area.querySelector(".h3-nle-timeline-toolbar-stack")),
        banner: box(area.querySelector(".h3-nle-conflict-banner")),
        ruler: box(area.querySelector(".h3-nle-ruler")),
        tracks: box(area.querySelector(".h3-nle-tracks")),
        footer: box(area.querySelector(".h3-nle-timeline-footer")),
        note: box(area.querySelector("#h3-nle-trim-instructions")),
        status: box(area.querySelector('[data-h3-nle-status="timeline"]')),
        header: box(document.querySelector(".h3-nle-header")),
        save: box(document.querySelector(".h3-nle-header .h3-nle-save")),
      };
    });
  const footer = await measure();
  const detail = JSON.stringify(footer);
  expect(footer.region, detail).not.toBeNull();
  expect(footer.scroller, detail).not.toBeNull();
  expect(footer.tracks, detail).not.toBeNull();
  expect(footer.note, detail).not.toBeNull();
  expect(footer.status, detail).not.toBeNull();
  expect(footer.section, detail).not.toBeNull();
  // No footer row remains, and the instructions occupy no visible line.
  expect(footer.footer, detail).toBeNull();
  expect(footer.note!.bottom - footer.note!.top, detail).toBeLessThanOrEqual(1);
  // The tracks viewport keeps its full minimum.
  expect(
    footer.tracks!.bottom - footer.tracks!.top,
    detail,
  ).toBeGreaterThanOrEqual(112);
  // The timeline section ends inside R4, and the status occupies no visible line there.
  expect(footer.section!.bottom, detail).toBeLessThanOrEqual(
    footer.region!.bottom,
  );
  expect(
    footer.status!.bottom - footer.status!.top,
    detail,
  ).toBeLessThanOrEqual(1);
  expect(
    footer.status!.right - footer.status!.left,
    detail,
  ).toBeLessThanOrEqual(1);
  // The save indicator is inside the top bar's row, and it reads "Saved" without a revision.
  expect(footer.save, detail).not.toBeNull();
  expect(footer.save!.top, detail).toBeGreaterThanOrEqual(footer.header!.top);
  expect(footer.save!.bottom, detail).toBeLessThanOrEqual(
    footer.header!.bottom,
  );
  await expect(overlay.locator(".h3-nle-header .h3-nle-save")).toHaveText(
    "Saved",
  );
  // At the reference size R4 shows its whole content; a scroller here is a layout defect.
  expect(footer.scroller!.scrollHeight, detail).toBeLessThanOrEqual(
    footer.scroller!.clientHeight,
  );
  // A populated, selected timeline changes the tracks and the inspector, not the footer.
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await overlay
    .locator('[data-h3-nle-asset] [data-h3-nle-control="asset.insert"]')
    .first()
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(1);
  await overlay
    .locator('[data-h3-nle-clip] [data-h3-nle-control="selection.set"]')
    .first()
    .click();
  await expect.poll(async () => (await shellSnapshot(page)).receipts).toBe(2);
  const selected = await measure();
  const selectedDetail = JSON.stringify(selected);
  // M25-62 (R7): a selected clip this wide trims with its inline grips, so the selection adds no
  // rail row and R4 still shows its whole content.
  expect(
    await overlay
      .locator('[data-h3-nle-clip][data-selected="true"] .h3-nle-grip')
      .count(),
    selectedDetail,
  ).toBe(2);
  expect(selected.rail, selectedDetail).toBeNull();
  expect(selected.section!.bottom, selectedDetail).toBeLessThanOrEqual(
    selected.region!.bottom,
  );
  expect(selected.scroller!.scrollHeight, selectedDetail).toBeLessThanOrEqual(
    selected.scroller!.clientHeight,
  );
  expect(
    selected.status!.bottom - selected.status!.top,
    selectedDetail,
  ).toBeLessThanOrEqual(1);
});

test("M25-53 a populated timeline scrolls inside its tracks grid and a toolbar tip never takes the ruler's seek", async ({
  page,
}) => {
  await openIntegratedShell(page, "virtualized");
  const overlay = page.locator(shellSurface);
  const region = overlay.locator('[data-h3-nle-area="timeline"]');
  const geometry = await region.evaluate((area) => {
    const scroller = area.querySelector<HTMLElement>(".h3-nle-timeline-area")!;
    const tracks = area.querySelector<HTMLElement>(".h3-nle-tracks")!;
    const section = area.querySelector<HTMLElement>(".h3-nle-timeline")!;
    return {
      areaScroll: scroller.scrollHeight,
      areaClient: scroller.clientHeight,
      tracksScroll: tracks.scrollHeight,
      tracksClient: tracks.clientHeight,
      mounted: Number(tracks.getAttribute("data-h3-nle-virtual-rows")),
      rows: Number(tracks.getAttribute("aria-rowcount")),
      sectionBottom: Math.round(section.getBoundingClientRect().bottom),
      regionBottom: Math.round(area.getBoundingClientRect().bottom),
    };
  });
  const detail = JSON.stringify(geometry);
  // D-5: a timeline taller than the tracks viewport scrolls inside the grid, never as R4, so
  // the row virtualizer mounts only the rows that viewport shows and the section stays in R4.
  expect(geometry.tracksScroll, detail).toBeGreaterThan(geometry.tracksClient);
  expect(geometry.areaScroll, detail).toBeLessThanOrEqual(geometry.areaClient);
  expect(geometry.mounted, detail).toBeLessThan(geometry.rows);
  expect(geometry.sectionBottom, detail).toBeLessThanOrEqual(
    geometry.regionBottom,
  );
  // D-4: a toolbar click leaves that button's hover description open under the pointer, right
  // above the ruler; the ruler click that follows must still reach the ruler and seek.
  await overlay.locator('[data-h3-nle-control="transport.ripple"]').click();
  const slider = playheadSlider(page);
  await expect(slider).toHaveAttribute("aria-disabled", "false");
  const target = await seekPlayhead(page, slider, 12);
  await expect(slider).toHaveAttribute("aria-valuenow", String(target));
  const owners = await slider.evaluate((element) => {
    const box = element.getBoundingClientRect();
    return document
      .elementsFromPoint(box.left + box.width / 2, box.top + box.height / 2)
      .map((node) => node.className)
      .filter((name) => typeof name === "string");
  });
  expect(owners, JSON.stringify(owners)).not.toContain("h3-icon-tip");
});

// B-M2563-05 and B-M2564-03: the reference fixture's `vid-timing` source runs at 12 fps in a
// 1/12288 time base on a 24 fps timeline, with embedded audio. "+" inserts it longer in output
// frames than the source has frames. The filmstrip and waveform painters once both refused that by
// adding output frames to source frames, and threw on every timeline draw; no journey listened
// for page errors while one drew.
test("M25-53 a different-rate clip longer than its source draws its filmstrip and waveform without a page error", async ({
  page,
}) => {
  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  await openIntegratedShell(page, "reference-preload");
  const overlay = page.locator(shellSurface);
  await overlay.locator('[data-h3-nle-pane="assets"]').click();
  await overlay
    .locator(
      '[data-h3-nle-asset="vid-timing"] [data-h3-nle-control="asset.insert"]',
    )
    .click();
  await expect(overlay.locator("[data-h3-nle-clip]")).toHaveCount(1);
  const timeline = (await shellSnapshot(page)).timelineSnapshot!;
  const clip = timeline.clips[0]!;
  const asset = timeline.assets.find((item) => item.assetId === "vid-timing")!;
  expect(clip.assetId).toBe("vid-timing");
  expect(clip.durationFrames).toBeGreaterThan(asset.sourceFrameCount!);
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            window.nleShellHarness.snapshot().mediaOwnership.filmstripBitmaps,
        ),
      { timeout: 20_000 },
    )
    .toBeGreaterThanOrEqual(1);
  // Draw again with the sprite in hand: zoom to fit, then let two frames paint.
  await overlay.locator('[data-h3-nle-control="transport.zoom_fit"]').click();
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  expect(browserErrors).toEqual([]);
});
