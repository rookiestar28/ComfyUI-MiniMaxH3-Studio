import { expect, test } from "../fixtures/h3Page";
import {
  installPinnedHostPalette,
  worstContrastWithin,
} from "../helpers/contrast";
import {
  armNextPaintSample,
  armRevisionSample,
  readOwnedInteractionSample,
} from "../helpers/assertions";

test.beforeEach(async ({ page }) => {
  await page.goto("/?mode=production");
});

test("64 segments and 65 outputs stay within render and DOM ceilings", async ({
  page,
}) => {
  const showMaximum = page.getByRole("button", { name: "Show maximum" });
  await showMaximum.click();
  await expect(page.getByTestId("production-timeline-segment")).toHaveCount(64);
  await expect(
    page.locator(".h3p-l > section").nth(3).locator("li"),
  ).toHaveCount(65);
  const renderSamples: number[] = [];
  for (let index = 0; index < 20; index += 1) {
    // IMPORTANT: the budget owns browser work from the actual click onward;
    // Playwright driver scheduling is external and can exceed the whole limit.
    await armNextPaintSample(showMaximum);
    await showMaximum.click();
    await expect(page.getByTestId("production-timeline-segment")).toHaveCount(
      64,
    );
    renderSamples.push(await readOwnedInteractionSample(page));
  }
  renderSamples.sort((left, right) => left - right);
  expect(renderSamples[18]).toBeLessThanOrEqual(500);
  const productionElements = await page.locator(".h3p *").count();
  expect(productionElements).toBeLessThanOrEqual(2048);
  // M17-23 Guard B / AC-M17-23-07. Windowing drops this page from 2004
  // elements to 1240, so the page total stops being a live guard against
  // per-segment density: anyone adding a row to the card would now have ~800
  // elements of slack to hide in. The binding contract from here on is the
  // per-item cost, which keeps working at any window size. Growing either
  // number is a deliberate edit in a reviewed diff, not a silent draw on
  // shared headroom.
  expect(productionElements).toBeLessThanOrEqual(1400);
  // M17-25 / AC-M17-25-10. The card grew a delivered-duration readout, and a
  // snapped segment grows a marker beside it, so the per-item budget is split
  // by snapped state rather than averaged: a card that shows the marker costs
  // one element more than one that does not, and both numbers are pinned. The
  // measured growth is +1 on every card and +1 again on a snapped card, which
  // at a 32-card window is 48 elements against roughly 160 of headroom.
  const perItem = await page.evaluate(() => {
    const count = (node: Element | null) =>
      node === null ? -1 : node.querySelectorAll("*").length + 1;
    const marked = (card: Element) =>
      card.querySelector("[data-testid='production-segment-snapped']") !== null;
    const cards = [...document.querySelectorAll(".h3p-s > li")];
    const plain = cards.filter(
      (card) => !card.hasAttribute("data-edit-target"),
    );
    const editTarget = document.querySelector("[data-edit-target]");
    return {
      renderedCards: cards.length,
      plainCardExact: count(plain.find((card) => !marked(card)) ?? null),
      plainCardSnapped: count(plain.find(marked) ?? null),
      editTargetCard: count(editTarget),
      // The edit target is segment 1, whose harness length is exact, so its
      // cost is the exact card plus the "Editing" badge and nothing else.
      editTargetSnapped: editTarget === null ? null : marked(editTarget),
      tile: count(
        document.querySelector("[data-testid='production-timeline-segment']"),
      ),
    };
  });
  // M21-03 AC-03. A collapsed row costs 12 elements against the 25 an M17-25
  // card cost, because the five state rows became one spine and the reorder
  // module moved to the one expanded row. The completed-run edit target costs
  // 28: it carries the full row plus the delivered-video dt/dd pair. Measured,
  // not predicted; the surrounding page and per-item ceilings remain pinned.
  expect(perItem).toEqual({
    renderedCards: 32,
    plainCardExact: 12,
    plainCardSnapped: 13,
    editTargetCard: 28,
    editTargetSnapped: false,
    tile: 3,
  });
  await expect(
    page.getByRole("button", { name: "Add current Context" }),
  ).toBeDisabled();

  // M21-03 AC-02: the reorder controls live on the one expanded row, so the
  // segment this loop moves is selected once before the loop rather than
  // reached for on a collapsed row twenty times.
  await page
    .getByRole("button", { name: "Select segment 2", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Move segment 2 left" }),
  ).toBeVisible();
  // Ordinals are positions, so the selected segment alternates between 2 and 1
  // as it is moved; the loop follows it rather than reaching for a control on
  // whichever collapsed row happens to be at ordinal 2. The count stays 20 and
  // ends where it started, which keeps every later assertion on the same board.
  const updateSamples: number[] = [];
  for (let index = 0; index < 20; index += 1) {
    const expectedRevision = 10 + index;
    const move = page.getByRole("button", {
      name: index % 2 === 0 ? "Move segment 2 left" : "Move segment 1 right",
    });
    await armRevisionSample(move, expectedRevision);
    await move.click();
    await expect(page.getByLabel(`revision ${expectedRevision}`)).toBeVisible();
    updateSamples.push(await readOwnedInteractionSample(page));
  }
  updateSamples.sort((left, right) => left - right);
  expect(updateSamples[18]).toBeLessThanOrEqual(250);

  const elementCountBeforeCycles = await page.locator(".h3p *").count();
  for (let index = 0; index < 25; index += 1) {
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("button", { name: "Production" }).click();
  }
  await expect(page.getByTestId("production-timeline-segment")).toHaveCount(64);
  expect(await page.locator(".h3p *").count()).toBe(elementCountBeforeCycles);
});

// M17-21 D7 / AC-M17-21-09. Every earlier visual review of this panel sampled
// 320/400/560 -- all below `MIN_SIDEBAR_WIDTH_PX = 704` -- so none of them saw
// the shipped layout. The floor is the design target and is asserted here.
test("segment state grid holds at the product floor", async ({ page }) => {
  await page.getByRole("button", { name: "Production" }).click();

  // The widest label the segment-card vocabulary can produce. Forcing it into
  // every chip pins the layout against the vocabulary rather than against
  // whichever states the fixture happens to carry.
  const WIDEST_LABEL = "Full recompute required";

  // M21-03 AC-01 removed the two-column page grid, so at the 704px product floor
  // the segment card is the full column rather than its left track and the
  // 31rem container query is satisfied there. The narrow case moves to the
  // declared 480px breakpoint, which is where one value track is still correct.
  const cases = [
    { sidebar: 480, valueTracks: 1 },
    { sidebar: 704, valueTracks: 2 },
    { sidebar: 960, valueTracks: 2 },
  ] as const;

  for (const item of cases) {
    await page.setViewportSize({ width: item.sidebar + 360, height: 1000 });
    await page.locator("#production-sidebar-container").evaluate((node, w) => {
      (node as HTMLElement).style.width = `${w}px`;
      (node as HTMLElement).style.maxWidth = "none";
    }, item.sidebar);

    const measured = await page.evaluate((label) => {
      const card = document.querySelector<HTMLElement>(".h3p-s > li");
      if (card === null) throw new Error("segment card unavailable");
      const grid = card.querySelector<HTMLElement>(".h3p-ss");
      if (grid === null) throw new Error("state grid unavailable");
      const chips = Array.from(card.querySelectorAll<HTMLElement>(".h3p-c"));
      const original = chips.map((chip) => chip.textContent ?? "");
      for (const chip of chips) chip.textContent = label;

      // A chip is on one line when its rendered height equals the height the
      // same chip has with wrapping disabled.
      const probe = chips[0].cloneNode(true) as HTMLElement;
      probe.style.position = "fixed";
      probe.style.left = "-9999px";
      probe.style.whiteSpace = "nowrap";
      document.body.append(probe);
      const singleLine = Math.round(probe.getBoundingClientRect().height);
      probe.remove();

      const heights = chips.map((chip) =>
        Math.round(chip.getBoundingClientRect().height),
      );
      const tracks = getComputedStyle(grid).gridTemplateColumns.split(" ");
      chips.forEach((chip, index) => {
        chip.textContent = original[index];
      });
      return { singleLine, heights, trackCount: tracks.length };
    }, WIDEST_LABEL);

    // Two tracks per key/value pair.
    expect(measured.trackCount).toBe(item.valueTracks * 2);
    // Every chip renders on one line, so no label breaks mid-word.
    for (const height of measured.heights) {
      expect(height).toBe(measured.singleLine);
    }
    // Uniform row rhythm within the card.
    expect(new Set(measured.heights).size).toBe(1);

    const overflow = await page
      .locator(".h3c")
      .evaluate((node) => node.scrollWidth - node.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  }
});

// M17-25 / AC-M17-25-06 and test plan item 7. A segment length is now what the
// host will actually deliver, and a request the 17-frame lattice moved is marked
// as moved. Both are asserted where a reader meets them -- the card, the
// timeline tile and the Sequence total -- and then again at the supported
// container widths, because a readout that wraps or clips at the product floor
// is not a readout.
test("the card, tile and Sequence show the delivered duration and mark a snap", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Production" }).click();

  // Harness lengths: 5167 ms lands on the lattice, 4167 ms is delivered as
  // 4458 ms, 8000 ms lands on the lattice. The card shows what the segment
  // produces; it never echoes the request back.
  const durations = page.getByTestId("production-segment-duration");
  await expect(durations).toHaveCount(3);
  await expect(durations.nth(0)).toHaveText("5.17 s");
  await expect(durations.nth(1)).toHaveText("4.46 s");
  await expect(durations.nth(2)).toHaveText("8.00 s");

  // Only the moved request is marked, and the mark explains itself: both
  // lengths and the delivered frame count, so the move is inspectable rather
  // than merely announced.
  const marks = page.getByTestId("production-segment-snapped");
  await expect(marks).toHaveCount(1);
  await expect(marks.first()).toHaveText("snapped");
  await expect(durations.nth(1)).toHaveAttribute(
    "title",
    "Requested 4.17 s; delivers 4.46 s (107 frames)",
  );
  await expect(durations.nth(0)).not.toHaveAttribute("title", /./);

  // The timeline carries the same delivered value, and a tile share of the
  // track is the derived frame count rather than the request, so no segment can
  // occupy a width its length would never produce.
  const tiles = page.getByTestId("production-timeline-segment");
  await expect(tiles.nth(1).locator("small")).toHaveText("4.46 s snapped");
  await expect(tiles.nth(1)).toHaveAttribute("data-snapped", "true");
  await expect(tiles.nth(0)).not.toHaveAttribute("data-snapped", /./);
  expect(
    await tiles.nth(1).evaluate((node) => getComputedStyle(node).flexGrow),
  ).toBe("107");

  // The Sequence total sums delivered lengths -- 5167 + 4458 + 8000 ms -- so
  // the header agrees with the cards beneath it instead of reporting a total
  // the host will never render.
  await expect(page.locator(".h3p-meta").first()).toHaveText("17.63 s total");

  for (const width of [704, 960]) {
    await page.setViewportSize({ width: width + 360, height: 1000 });
    await page.locator("#production-sidebar-container").evaluate((node, w) => {
      (node as HTMLElement).style.width = `${w}px`;
      (node as HTMLElement).style.maxWidth = "none";
    }, width);
    const measured = await page.evaluate(() => {
      const card = document.querySelectorAll(".h3p-s > li")[1] as HTMLElement;
      const pick = (selector: string) => {
        const node = card.querySelector<HTMLElement>(selector);
        if (node === null) throw new Error(`missing ${selector}`);
        return node;
      };
      const summary = pick(".h3p-sf");
      const duration = pick(".h3p-du");
      const mark = pick(".h3p-sn");
      const mode = pick(".h3p-mode");
      // The three chips are different heights -- only the mode and the mark
      // carry a border -- so a shared top edge would be the wrong test. What
      // "one row" means here is a shared vertical centre, which is what
      // `align-items: center` produces, plus a summary row no taller than its
      // tallest child, which is what not wrapping produces.
      const middle = (node: HTMLElement) => {
        const box = node.getBoundingClientRect();
        return Math.round(box.top + box.height / 2);
      };
      const height = (node: HTMLElement) =>
        Math.round(node.getBoundingClientRect().height);
      return {
        sameRow:
          middle(duration) === middle(mode) && middle(mark) === middle(mode),
        oneLine:
          height(summary) ===
          Math.max(height(mode), height(duration), height(mark)),
        clipped:
          duration.scrollWidth > duration.clientWidth ||
          mark.scrollWidth > mark.clientWidth,
      };
    });
    expect(measured).toEqual({ sameRow: true, oneLine: true, clipped: false });
    const overflow = await page
      .locator(".h3c")
      .evaluate((node) => node.scrollWidth - node.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  }
});

// M17-22 / AC-M17-22-01..02. The light palette used to key on `data-theme` and
// `.light-theme`, neither of which ComfyUI sets, so it was dead code. These
// rows exercise the real cascade rather than the presence of a selector.
test("theme follows the host signal and yields to an explicit override", async ({
  page,
}) => {
  const shell = page.locator(".h3c");
  // M21-03 replaced the M17-12 neutral ladder; these are the new canvases.
  const DARK = "color(srgb 0.0509804 0.0588235 0.0745098 / 0.28)";
  const LIGHT = "color(srgb 0.92549 0.937255 0.956863 / 0.28)";

  const setHost = (present: boolean, dark: boolean, explicit: string | null) =>
    page.evaluate(
      ({ present: hostPresent, dark: hostDark, explicit: override }) => {
        document.body.classList.toggle("litegraph", hostPresent);
        document.documentElement.classList.toggle("dark-theme", hostDark);
        if (override === null)
          document.documentElement.removeAttribute("data-theme");
        else document.documentElement.setAttribute("data-theme", override);
      },
      { present, dark, explicit },
    );

  // No host at all: the accepted M17-21 dark appearance, unchanged.
  await setHost(false, false, null);
  await expect(shell).toHaveCSS("background-color", DARK);

  // Host present and not signalling dark: ComfyUI is in light mode.
  await setHost(true, false, null);
  await expect(shell).toHaveCSS("background-color", LIGHT);

  // Host signalling dark.
  await setHost(true, true, null);
  await expect(shell).toHaveCSS("background-color", DARK);

  // An explicit override beats the host in both directions.
  await setHost(true, true, "light");
  await expect(shell).toHaveCSS("background-color", LIGHT);
  await setHost(true, false, "dark");
  await expect(shell).toHaveCSS("background-color", DARK);

  // A host-light panel must not inherit dark-only accents: the stage colours
  // and the danger colour were never contrast-audited on a light surface.
  await setHost(true, false, null);
  const accents = await shell.evaluate((node) => {
    const cs = getComputedStyle(node);
    return {
      intent: cs.getPropertyValue("--h3-stage-intent").trim(),
      execute: cs.getPropertyValue("--h3-stage-execute").trim(),
      danger: cs.getPropertyValue("--dg").trim(),
    };
  });
  // `--dg` is the audited danger *role*, #a32307, not the frozen M17-12 identity
  // literal #b72a0d it briefly shared: on --h3-control, which backs the stage
  // tab whose blocked child uses this colour, #b72a0d scores 4.466 and #a32307
  // scores 5.343. The identity literal is unmoved and still on
  // --h3-signal-orange, which the frozen-palette row pins.
  expect(accents).toEqual({
    intent: "#6b38ff",
    execute: "#177444",
    danger: "#a32307",
  });

  await setHost(false, false, null);
});

// M17-22 / AC-M17-22-03. The light palette shipped for three items without
// ever rendering, so none of its values had been measured against a light
// surface; the dark palette's dim and idle tokens had the same gap. This sweeps
// every leaf text node on all three pages in both themes.
test("text contrast clears WCAG AA in both themes", async ({ page }) => {
  // M17-22 corrective. This sweep used to run against a document where none of
  // the host's variables existed, so every `var(--host, literal)` in the panel
  // resolved to its literal and the sweep could only ever measure the safe half
  // of the pair. `--dg` was `var(--error-text, #ff8585)`: the fallback scores
  // 5.641 on --h3-control and passed here, while the host's actual #ff4444
  // scores 3.886 and shipped. The pinned host's real values are installed first
  // so the sweep measures what a user sees, and any future derivation from a
  // host colour is caught by the assertion rather than by a later reviewer.
  await installPinnedHostPalette(page);

  for (const dark of [true, false]) {
    await page.evaluate((isDark) => {
      document.body.classList.add("litegraph");
      document.documentElement.classList.toggle("dark-theme", isDark);
    }, dark);
    for (const name of ["Context", "Production", "Settings"]) {
      await page.getByRole("button", { name, exact: true }).click();
      const worst = await worstContrastWithin(page, ".h3c");
      expect(
        worst.ratio,
        `${dark ? "dark" : "light"} / ${name}: "${worst.text}" at ${worst.color}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  }

  await page.evaluate(() => {
    document.body.classList.remove("litegraph");
    document.documentElement.classList.remove("dark-theme");
  });
});

// M17-23 / AC-M17-23-02, AC-M17-23-03, AC-M17-23-04. `MAX_WORKSPACE_SEGMENTS`
// is 64 and the list used to render every one of them: 13 528px of cards inside
// a 14 552px page at the 704px product floor, measured before this item. The
// list is now a page of at most `MAX_RENDERED_LIST_ITEMS`, and because a bound
// that is not reported is a silent truncation, the visible range and the global
// total are both stated.
test("the segment list renders a bounded, reported window", async ({
  page,
}) => {
  await page.locator("#production-sidebar-container").evaluate((node) => {
    (node as HTMLElement).style.width = "704px";
  });
  await page.getByRole("button", { name: "Show maximum" }).click();
  await expect(page.getByTestId("production-timeline-segment")).toHaveCount(64);

  const range = page
    .getByRole("region", { name: "Segment" })
    .getByText(/^Showing /);
  await expect(page.locator(".h3p-s > li")).toHaveCount(32);
  await expect(range).toHaveText("Showing 1–32 of 64 segments · 1 selected");

  // The bound is on elements, not on height, so the region scrolls internally.
  // Without this the page would still be about 6 800px of column and the Run
  // region would sit below all of it.
  const listGeometry = await page.locator(".h3p-s").evaluate((node) => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
  }));
  expect(listGeometry.overflowY).toBe("auto");
  expect(listGeometry.clientHeight).toBeLessThanOrEqual(720);
  expect(listGeometry.scrollHeight).toBeGreaterThan(listGeometry.clientHeight);
  const pageScrollHeight = await page
    .locator(".h3p")
    .evaluate((node) => node.scrollHeight);
  expect(pageScrollHeight).toBeLessThanOrEqual(4500);

  // Nothing is unreachable: the pager crosses the boundary, and so does the
  // timeline, which is never windowed precisely so that it can.
  await page.getByRole("button", { name: "Show later segments" }).click();
  await expect(range).toHaveText("Showing 33–64 of 64 segments · 1 selected");
  await expect(page.locator(".h3p-s > li > label").first()).toHaveText(
    "Segment 33",
  );
  await expect(
    page.getByRole("button", { name: "Show later segments" }),
  ).toBeDisabled();

  await page
    .getByRole("button", { name: "Select segment 5", exact: true })
    .click();
  await expect(page.getByLabel("revision 9")).toBeVisible();
  await expect(range).toHaveText("Showing 1–32 of 64 segments · 1 selected");
  await expect(page.locator("[data-edit-target] > label")).toHaveText(
    "Segment 5",
  );
});

// M17-23 / AC-M17-23-08. This is the failure mode windowing introduces and that
// none of the rejected alternatives shared: a move can carry a segment onto the
// other page, and a window anchored on whichever page the user happened to be
// reading would let the segment they are working on vanish. The anchor is the
// moved segment's identity, so the window follows it. Ordinals are positions,
// so the segment that was 33 is 32 afterwards; identity is what is tracked.
test("a reorder across the window boundary keeps the moved segment on screen", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Show maximum" }).click();
  await expect(page.getByTestId("production-timeline-segment")).toHaveCount(64);
  const range = page
    .getByRole("region", { name: "Segment" })
    .getByText(/^Showing /);
  const cards = page.locator(".h3p-s > li > label");

  await page.getByRole("button", { name: "Show later segments" }).click();
  await expect(range).toHaveText("Showing 33–64 of 64 segments · 1 selected");

  // M21-03 AC-02: the reorder controls belong to the one expanded row, so the
  // segment being moved is selected first. The sequence track reaches a segment
  // on either page, which is what makes this still one gesture away.
  // Selecting is itself a workspace mutation here, so each revision below is
  // asserted where it happens rather than letting a later poll catch an
  // intermediate value and pass for the wrong reason.
  await page
    .getByRole("button", { name: "Select segment 33", exact: true })
    .click();
  await expect(page.getByLabel("revision 9")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Move segment 33 left" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Move segment 33 left" }).click();
  await expect(page.getByLabel("revision 10")).toBeVisible();
  await expect(range).toHaveText("Showing 1–32 of 64 segments · 1 selected");
  await expect(cards).toHaveCount(32);
  await expect(cards.last()).toHaveText("Segment 32");

  // The same guarantee through the position field rather than the move buttons.
  await page.getByRole("button", { name: "Show later segments" }).click();
  await expect(range).toHaveText("Showing 33–64 of 64 segments · 1 selected");
  // The position field is on the expanded row, so segment 40 is selected first.
  await page
    .getByRole("button", { name: "Select segment 40", exact: true })
    .click();
  await expect(page.getByLabel("revision 11")).toBeVisible();
  await page
    .getByRole("spinbutton", { name: "Move segment 40 to position" })
    .fill("3");
  await page
    .getByRole("button", { name: "Apply position for segment 40" })
    .click();
  await expect(page.getByLabel("revision 12")).toBeVisible();
  await expect(range).toHaveText("Showing 1–32 of 64 segments · 1 selected");
});

// M17-23 / AC-M17-23-01. The removed defect: `selectedSegmentIds` is a genuine
// set, but Replace, Delete and the relationship editor acted on its first
// member in sequence order, so selecting three segments and pressing Delete
// removed one the user never identified. A destructive action now has exactly
// one named target or none at all.
test("destructive and replacing actions name one identified target", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Show fault states" }).click();
  await expect(page.locator(".h3p-s > li")).toHaveCount(3);

  await expect(
    page.getByRole("button", { name: "Delete segment 1" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", {
      name: "Replace segment 1 with current Context",
    }),
  ).toBeEnabled();
  await expect(page.locator("[data-edit-target]")).toHaveCount(1);
  await expect(page.locator('[aria-current="true"]')).toHaveCount(1);
  await expect(page.locator(".h3p-et")).toHaveText("Editing");

  // Three selected: no single target, so nothing destructive is offered.
  await page.getByRole("checkbox", { name: "Segment 2", exact: true }).click();
  await expect(page.getByLabel("revision 8")).toBeVisible();
  await page.getByRole("checkbox", { name: "Segment 3", exact: true }).click();
  await expect(page.getByLabel("revision 9")).toBeVisible();

  await expect(page.locator("[data-edit-target]")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Delete segment" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Replace with current Context" }),
  ).toBeDisabled();
  await expect(page.locator(".h3p-r")).toHaveCount(0);
  await expect(
    page.getByText(
      "Select exactly one segment to replace, delete or relate it.",
    ),
  ).toBeVisible();
  // The one action that genuinely honours a set says so and counts the set.
  await expect(
    page.getByRole("button", { name: "Understand selected (3)" }),
  ).toBeVisible();
});

// M17-23 Guard A / AC-M17-23-06. The DOM may be optimized freely; the
// accessible tree is the contract. M17-21 shaved four elements per card against
// the DOM ceiling and twice reached for savings that would have cost semantics
// -- rendering visible state keys through `::before { content: attr(...) }`,
// and unwrapping the position `label` -- and neither would have failed a test,
// because `aria-label` survives removal of the `label` element. It does not
// survive these.
test("the accessible tree of a segment card is a contract", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Show fault states" }).click();
  const cards = page.locator(".h3p-s > li");
  await expect(cards).toHaveCount(3);

  await expect(cards.first()).toMatchAriaSnapshot(`
    - listitem:
      - checkbox "Segment 1" [checked]
      - text: Segment 1 Editing t2va 5.17 s
      - term: Closure
      - definition: Not available
      - term: Job
      - definition: Not available
      - term: Artifact
      - definition: Not available
      - term: Continuity
      - definition: Not available
      - term: Boundary
      - definition: Independent
      - text: Move
      - button "Move segment 1 left" [disabled]: ←
      - button "Move segment 1 right": →
      - text: Position
      - spinbutton "Move segment 1 to position": "1"
      - button "Apply position for segment 1": Apply
  `);

  // M21-03 AC-02. An unselected segment is one row. The five state rows became a
  // spine, and the spine's accessible name still conveys all five states in the
  // same fixed order -- which is exactly what this contract exists to protect.
  await expect(cards.nth(1)).toMatchAriaSnapshot(`
    - listitem:
      - checkbox "Segment 2"
      - text: Segment 2 t2va 4.46 s snapped
      - img "Full recompute required, Failed, Failed, Not available, Independent"
  `);

  // Guard A has one blind spot, and it is worth stating rather than leaving for
  // the next author to rediscover: the accessible tree flattens generic
  // containers, so unwrapping the position `label` into a `span` leaves both
  // snapshots above byte-identical -- the input keeps its name from
  // `aria-label`. That is exactly the saving M17-21 implemented and measured
  // before a visual check caught "Position" wrapping onto a different line from
  // the field it names. Verified during M17-23 implementation: with the label
  // unwrapped, all 429 unit tests and all 20 browser rows still passed. The
  // association is a structural contract, so it is asserted structurally.
  const labelBindings = await page.evaluate(() => {
    const card = document.querySelector(".h3p-s > li");
    const position = card?.querySelector("input[type='number']");
    const checkbox = card?.querySelector("input[type='checkbox']");
    const keyOf = (input: Element | null | undefined) =>
      input?.closest("label")?.querySelector(".h3p-k")?.textContent ?? null;
    return {
      positionKey: keyOf(position),
      positionLabelled: position?.closest("label") !== null,
      checkboxLabelled: checkbox?.closest("label") !== null,
    };
  });
  expect(labelBindings).toEqual({
    positionKey: "Position",
    positionLabelled: true,
    checkboxLabelled: true,
  });
});

// M17-23, distinct review finding 1. The window anchor is component-local state
// keyed on a segment identifier, and nothing used to clear it. The fall-through
// in the anchor lookup makes a stale identifier *look* harmless -- `findIndex`
// returns -1 and the anchor drops to the selection -- but that only holds while
// the identifier is absent. A workspace that is released and rebuilt can mint
// the same identifier again, and the anchor then resurrects and opens the list
// on a page the user never asked for. Pruning it on projection change is what
// makes the fall-through argument actually sound.
test("a window anchor does not survive the workspace it belongs to", async ({
  page,
}) => {
  const range = page
    .getByRole("region", { name: "Segment" })
    .getByText(/^Showing /);

  await page.getByRole("button", { name: "Show maximum" }).click();
  await expect(page.getByTestId("production-timeline-segment")).toHaveCount(64);
  await page.getByRole("button", { name: "Show later segments" }).click();
  await expect(range).toHaveText("Showing 33–64 of 64 segments · 1 selected");

  // A different workspace, which does not contain the anchored segment.
  await page.getByRole("button", { name: "Show previewable" }).click();
  await expect(page.locator(".h3p-s > li")).toHaveCount(3);
  await expect(range).toHaveText("Showing 1–3 of 3 segments · 1 selected");

  // Rebuilding the large workspace mints the same identifiers again. The list
  // must open where the selection is, not where a dead anchor used to point.
  await page.getByRole("button", { name: "Show maximum" }).click();
  await expect(page.getByTestId("production-timeline-segment")).toHaveCount(64);
  await expect(range).toHaveText("Showing 1–32 of 64 segments · 1 selected");
  await expect(page.locator("[data-edit-target] > label")).toHaveText(
    "Segment 1",
  );
});
